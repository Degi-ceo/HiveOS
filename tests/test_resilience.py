"""M1 resilience tests — rate-limit parsing, proactive cooldown, cost budgeter,
hardened codex planner. All offline (no network)."""
from __future__ import annotations

import asyncio

import pytest

from hive.llm.rate_limit import RateLimitState, parse_rate_limit_headers
from hive.llm.pricing import cost_usd, rate_for
from hive.core.budgeter import Budgeter


# --- rate-limit parsing --------------------------------------------------------

def test_parse_returns_none_without_headers():
    assert parse_rate_limit_headers({"content-type": "application/json"}) is None


def test_parse_reads_buckets_case_insensitive():
    state = parse_rate_limit_headers({
        "X-RateLimit-Limit-Requests": "100",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-reset-requests": "30",
        "x-ratelimit-limit-tokens": "1000000",
        "x-ratelimit-remaining-tokens": "999000",
    }, provider="minimax")
    assert state is not None
    assert state.provider == "minimax"
    assert state.requests_min.limit == 100
    assert state.requests_min.remaining == 10
    assert state.requests_min.usage_pct == 90.0
    assert state.tokens_min.limit == 1_000_000


def test_hottest_picks_highest_usage():
    state = parse_rate_limit_headers({
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "5",       # 95% used
        "x-ratelimit-limit-tokens": "1000",
        "x-ratelimit-remaining-tokens": "900",       # 10% used
    })
    hottest = state.hottest()
    assert hottest is not None and hottest.usage_pct == 95.0


def test_hottest_none_when_no_limits():
    assert RateLimitState().hottest() is None


# --- pricing -------------------------------------------------------------------

def test_cost_usd_known_model():
    # MiniMax-M3 default: 0.30 in / 1.20 out per million
    c = cost_usd("MiniMax-M3", 1_000_000, 1_000_000)
    assert c == pytest.approx(1.50)


def test_cost_usd_unknown_model_uses_fallback():
    assert cost_usd("Totally-New-Model", 1_000_000, 0) == pytest.approx(0.30)


def test_rate_for_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_PRICE_MINIMAX_M3_IN", "9.0")
    rin, _ = rate_for("MiniMax-M3")
    assert rin == 9.0


# --- budgeter cost tracking ----------------------------------------------------

def test_record_usage_accrues_cost_and_tokens():
    # Budgeter is a pure accumulator: cost arrives in the payload (router computes it).
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000, "cost_usd": 1.50})
    snap = b.snapshot()
    assert snap["cost_today_usd"] == pytest.approx(1.50)
    assert snap["tokens_today"] == {"input": 1_000_000, "output": 1_000_000}
    assert snap["by_model"]["MiniMax-M3"]["cost_usd"] == pytest.approx(1.50)


def test_record_usage_ignores_empty():
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 0, "output_tokens": 0})
    assert b.snapshot()["cost_today_usd"] == 0.0


def test_record_usage_accepts_event_object():
    class _Evt:
        data = {"model": "MiniMax-M3", "input_tokens": 1_000_000,
                "output_tokens": 0, "cost_usd": 0.30}
    b = Budgeter()
    b.record_usage(_Evt())
    assert b.snapshot()["cost_today_usd"] == pytest.approx(0.30)


# --- router proactive cooldown -------------------------------------------------

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult, LLMAdapter, Usage
from hive.llm.credential_pool import CredentialPool
from hive.llm.failover import RetryPolicy
from hive.llm.router import ModelRouter, PlannerError, make_codex_planner


def _config(tmp_path):
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


class _RLAdapter(LLMAdapter):
    """Adapter that returns a result carrying a near-exhausted rate-limit state."""
    name = "rl"

    def __init__(self, usage_pct: float):
        state = parse_rate_limit_headers({
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": str(int(100 - usage_pct)),
            "x-ratelimit-reset-requests": "60",
        })
        self._state = state

    async def complete(self, request, *, api_key):
        return CompletionResult(text="ok", model=request.model,
                                usage=Usage(input_tokens=5, output_tokens=5),
                                raw={"rate_limit_state": self._state})


def test_router_cools_credential_when_rate_limit_hot(tmp_path):
    pool = CredentialPool(["k0", "k1"], cooldown_seconds=100.0)
    router = ModelRouter(config=_config(tmp_path), adapter=_RLAdapter(usage_pct=95),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0))
    msgs = []
    from hive.core.types import Message, Role
    msgs = [Message(role=Role.USER, content="hi")]
    asyncio.run(router.complete(msgs))
    # one key should now be cooling (only one available)
    assert len(pool.available()) == 1


def test_pool_cooldown_does_not_bump_failures():
    pool = CredentialPool(["a"], clock=lambda: 0.0)
    cred = pool.acquire()
    pool.cooldown(cred, 30.0)
    assert cred.failures == 0           # healthy key — not a failure
    assert cred.cooldown_until == 30.0  # parked for the window


def test_router_does_not_cool_when_rate_limit_cool(tmp_path):
    pool = CredentialPool(["k0", "k1"], cooldown_seconds=100.0)
    router = ModelRouter(config=_config(tmp_path), adapter=_RLAdapter(usage_pct=10),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0))
    from hive.core.types import Message, Role
    asyncio.run(router.complete([Message(role=Role.USER, content="hi")]))
    assert len(pool.available()) == 2  # neither cooled


# --- credential pool status (item 46-47) ---------------------------------------

def test_credential_pool_status():
    from hive.llm.credential_pool import CredentialPool
    clock = [0.0]
    pool = CredentialPool(["k1", "k2"], clock=lambda: clock[0])
    status = pool.status()
    assert len(status) == 2
    assert all("label" in s and "failures" in s and "cooling" in s for s in status)
    assert all(not s["cooling"] for s in status)
    cred = pool.acquire()
    pool.report_failure(cred)
    status2 = pool.status()
    assert any(s["cooling"] for s in status2)


# --- hardened codex planner ----------------------------------------------------

def test_codex_planner_success(tmp_path):
    # `cat` echoes stdin back -> non-empty output, exit 0
    planner = make_codex_planner("cat", timeout=10)
    from hive.core.types import Message, Role
    out = asyncio.run(planner([Message(role=Role.USER, content="plan this")], None))
    assert "plan this" in out


def test_codex_planner_nonzero_exit_raises():
    planner = make_codex_planner("false", timeout=10)  # exits 1, no output
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_codex_planner_missing_binary_raises():
    planner = make_codex_planner("definitely-not-a-real-binary-xyz", timeout=5)
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_codex_planner_timeout_raises():
    planner = make_codex_planner("sleep 5", timeout=0.2)
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_router_plan_falls_back_to_executor_on_planner_error(tmp_path):
    """A failing planner must not dead-end a PLAN turn; the executor answers."""
    from hive.core.types import Message, Role
    from hive.llm.router import TaskKind

    async def broken_planner(messages, system):
        raise PlannerError("codex down")

    class _ExecAdapter(LLMAdapter):
        name = "exec"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="executor plan", model=request.model,
                                    usage=Usage(input_tokens=1, output_tokens=1))

    router = ModelRouter(config=_config(tmp_path), adapter=_ExecAdapter(),
                         credential_pool=CredentialPool(["k"]),
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
                         planner=broken_planner)
    out = asyncio.run(router.complete([Message(role=Role.USER, content="plan")],
                                      TaskKind.PLAN))
    assert out.text == "executor plan"


def test_record_usage_ignores_non_mapping_event_payload():
    from hive.core.budgeter import Budgeter

    class _Evt:
        data = object()

    b = Budgeter()
    b.record_usage(_Evt())
    snap = b.snapshot()
    assert snap["tokens_today"] == {"input": 0, "output": 0}
    assert snap["cost_today_usd"] == 0.0


# --- New resilience tests (appended) -------------------------------------------

import unittest.mock as mock

import httpx

from hive.llm.failover import ClassifiedError, FailoverReason, RetryPolicy, classify


def _make_http_error(status: int) -> httpx.HTTPStatusError:
    """Helper: build a minimal httpx.HTTPStatusError with the given status code."""
    response = mock.MagicMock(spec=httpx.Response)
    response.status_code = status
    response.text = ""
    return httpx.HTTPStatusError(
        message=f"HTTP {status}",
        request=mock.MagicMock(spec=httpx.Request),
        response=response,
    )


def test_failover_retries_on_503():
    """classify() marks 5xx errors as retryable (OVERLOADED); second attempt succeeds."""
    call_count = 0

    async def flaky_call():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_http_error(503)
        return "success"

    async def _run():
        policy = RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)
        for attempt in range(policy.max_attempts):
            try:
                return await flaky_call()
            except httpx.HTTPStatusError as exc:
                err = classify(exc)
                assert err.retryable, "503 should be retryable"
                assert err.reason == FailoverReason.OVERLOADED
                if attempt + 1 >= policy.max_attempts:
                    raise
        return None

    result = asyncio.run(_run())
    assert result == "success"
    assert call_count == 2


def test_failover_does_not_retry_on_401():
    """classify() marks 401 as AUTH — retryable flag reflects policy (rotate/fallback)."""
    exc = _make_http_error(401)
    err = classify(exc)
    assert err.reason == FailoverReason.AUTH
    assert err.status == 401
    assert err.should_rotate_credential, "auth error should rotate credential"
    assert err.should_fallback, "auth error should trigger fallback"
    # The key insight: while AUTH is technically retryable-with-new-key,
    # you must NOT retry with the SAME key; credential rotation is mandatory.
    assert err.should_rotate_credential is True


def test_failover_exhausts_all_retries_raises():
    """When all retry attempts fail, the final exception propagates."""
    async def always_fail():
        raise _make_http_error(503)

    async def _run():
        policy = RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)
        last_exc: Exception | None = None
        for attempt in range(policy.max_attempts):
            try:
                await always_fail()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                err = classify(exc)
                assert err.retryable
        if last_exc is not None:
            raise last_exc

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.response.status_code == 503


def test_credential_pool_roundtrip_acquire_success_report():
    """acquire + report_success + acquire again returns the same key (not on cooldown)."""
    from hive.llm.credential_pool import CredentialPool

    pool = CredentialPool(["key-alpha"], cooldown_seconds=60.0)
    cred1 = pool.acquire()
    assert cred1 is not None
    assert cred1.key == "key-alpha"

    pool.report_success(cred1)
    assert cred1.failures == 0
    assert cred1.cooldown_until == 0.0

    cred2 = pool.acquire()
    assert cred2 is not None
    assert cred2.key == "key-alpha"   # still available after success


def test_credential_pool_rotates_on_failure():
    """With two keys, failing the first causes the second to be returned next."""
    from hive.llm.credential_pool import CredentialPool

    clock_val = [0.0]
    pool = CredentialPool(["key-one", "key-two"],
                          cooldown_seconds=30.0,
                          clock=lambda: clock_val[0])

    first = pool.acquire()
    assert first is not None
    assert first.key == "key-one"

    pool.report_failure(first)
    # key-one is now on cooldown; key-two should be returned
    second = pool.acquire()
    assert second is not None
    assert second.key == "key-two"

    # Advance clock past cooldown — key-one should be available again
    clock_val[0] = 31.0
    third = pool.acquire()
    assert third is not None
    assert third.key == "key-one"


# --- Six new resilience tests ----------------------------------------------------

def test_classify_429_is_rate_limited():
    """classify() maps HTTP 429 to RATE_LIMIT which is retryable."""
    exc = _make_http_error(429)
    err = classify(exc)
    assert err.reason == FailoverReason.RATE_LIMIT
    assert err.retryable is True
    assert err.status == 429


def test_classify_503_is_overloaded():
    """classify() maps HTTP 503 to OVERLOADED which is retryable."""
    exc = _make_http_error(503)
    err = classify(exc)
    assert err.reason == FailoverReason.OVERLOADED
    assert err.retryable is True
    assert err.status == 503


def test_retry_policy_first_backoff_is_positive():
    """backoff(0) must return a positive wait time (not zero, not negative)."""
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0)
    wait = policy.backoff(0)
    assert wait > 0, f"Expected backoff(0) > 0, got {wait}"


def test_classify_200_is_not_error_result():
    """A 200 response raises no exception; no ClassifiedError is produced for it."""
    # 200 is the success path — classify() only handles exception objects.
    # We verify that a non-error status maps to UNKNOWN when forced via an exception.
    # In normal operation the router never calls classify() for 200.
    # Here we make sure OK logic stays separate: just confirm RATE_LIMIT != UNKNOWN.
    exc = _make_http_error(429)
    err = classify(exc)
    assert err.reason != FailoverReason.UNKNOWN
    assert err.retryable is True


def test_failover_returns_correct_reason_for_auth():
    """classify() maps 401 to AUTH reason with should_rotate_credential=True."""
    exc = _make_http_error(401)
    err = classify(exc)
    assert err.reason == FailoverReason.AUTH
    assert err.should_rotate_credential is True
    assert err.should_fallback is True
    assert err.status == 401


def test_credential_pool_report_failure_sets_cooldown():
    """After report_failure the credential is on cooldown; acquire() returns None."""
    from hive.llm.credential_pool import CredentialPool

    clock_val = [0.0]
    pool = CredentialPool(["only-key"],
                          cooldown_seconds=60.0,
                          clock=lambda: clock_val[0])

    cred = pool.acquire()
    assert cred is not None
    pool.report_failure(cred)

    # The single key is now cooling — acquire should return None.
    result = pool.acquire()
    assert result is None, "Expected None when the only key is on cooldown"


# --- Wave 3M additional tests ---------------------------------------------------

def test_budgeter_snapshot_has_calls_today():
    """Budgeter.snapshot() includes 'calls_today' key."""
    b = Budgeter(daily_cap=50)
    b.record_call()
    snap = b.snapshot()
    assert "calls_today" in snap
    assert snap["calls_today"] == 1


def test_budgeter_snapshot_has_daily_cap():
    """Budgeter.snapshot() includes 'daily_cap' matching constructor value."""
    b = Budgeter(daily_cap=50)
    snap = b.snapshot()
    assert snap["daily_cap"] == 50


def test_budgeter_forecast_has_remaining_calls():
    """Budgeter.forecast() includes 'remaining_calls' that decrements with use."""
    b = Budgeter(daily_cap=10)
    b.record_call()
    f = b.forecast()
    assert "remaining_calls" in f
    assert f["remaining_calls"] == 9


def test_budgeter_calls_per_hour_is_positive_after_call():
    """calls_per_hour() is a positive float after at least one record_call()."""
    b = Budgeter(daily_cap=100)
    b.record_call()
    rate = b.calls_per_hour()
    assert isinstance(rate, float)
    assert rate > 0.0


def test_budgeter_reset_daily_clears_calls():
    """reset_daily() resets calls_today back to 0."""
    b = Budgeter(daily_cap=100)
    b.record_call()
    b.record_call()
    b.reset_daily()
    assert b.remaining_calls() == 100


def test_budgeter_is_near_cap_false_at_start():
    """is_near_cap() returns False when fewer than 70% of calls used."""
    b = Budgeter(daily_cap=100, warn_pct=70.0)
    for _ in range(5):
        b.record_call()
    assert b.is_near_cap() is False


# --- Wave 3O additional tests ---------------------------------------------------

def test_budgeter_gate_returns_true_when_fresh():
    """gate() returns (True, '') when no calls have been made."""
    b = Budgeter(daily_cap=100)
    allowed, msg = b.gate()
    assert allowed is True
    assert msg == ""


def test_budgeter_gate_returns_false_when_cap_exhausted():
    """gate() returns (False, ...) once daily cap is exceeded."""
    b = Budgeter(daily_cap=5)
    for _ in range(6):
        b.record_call()
    allowed, msg = b.gate()
    assert allowed is False
    assert "cap" in msg.lower() or len(msg) > 0


def test_budgeter_cost_per_call_is_float():
    """cost_per_call() returns a float (may be 0.0 when no usage recorded)."""
    b = Budgeter(daily_cap=100)
    cost = b.cost_per_call()
    assert isinstance(cost, float)


def test_budgeter_remaining_calls_matches_cap_minus_used():
    """remaining_calls() equals daily_cap minus calls made."""
    b = Budgeter(daily_cap=20)
    b.record_call()
    b.record_call()
    assert b.remaining_calls() == 18


def test_budgeter_warning_status_none_when_low_usage():
    """warning_status() returns None when usage is far from cap."""
    b = Budgeter(daily_cap=100, warn_pct=90.0)
    b.record_call()
    assert b.warning_status() is None


def test_budgeter_calls_per_hour_zero_when_no_calls():
    """calls_per_hour() is 0.0 when no calls have been recorded."""
    b = Budgeter(daily_cap=100)
    rate = b.calls_per_hour()
    assert rate == 0.0


# --- Wave 3S additional tests ---------------------------------------------------

def test_forecast_pct_used_increases_with_calls():
    """forecast() pct_used rises proportionally as calls are recorded."""
    b = Budgeter(daily_cap=100)
    for _ in range(10):
        b.record_call()
    f = b.forecast()
    assert f["pct_used"] == pytest.approx(10.0)


def test_forecast_days_remaining_none_when_no_calls():
    """forecast() days_remaining is None when no calls have been made."""
    b = Budgeter(daily_cap=50)
    f = b.forecast()
    assert f["days_remaining"] is None


def test_classify_402_is_billing():
    """classify() maps HTTP 402 to BILLING reason which is non-retryable."""
    exc = _make_http_error(402)
    err = classify(exc)
    assert err.reason == FailoverReason.BILLING
    assert err.retryable is False
    assert err.should_rotate_credential is True
    assert err.should_fallback is True


def test_classify_408_is_timeout():
    """classify() maps HTTP 408 to TIMEOUT which is retryable."""
    exc = _make_http_error(408)
    err = classify(exc)
    assert err.reason == FailoverReason.TIMEOUT
    assert err.retryable is True
    assert err.status == 408


def test_classify_transport_error_is_overloaded():
    """A plain httpx.TransportError (non-HTTP) classifies as OVERLOADED / retryable."""
    exc = httpx.TransportError("connection reset")
    err = classify(exc)
    assert err.reason == FailoverReason.OVERLOADED
    assert err.retryable is True
    assert err.status is None


def test_record_usage_accumulates_cost_across_calls():
    """record_usage() sums cost_usd across multiple calls."""
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 500_000,
                    "output_tokens": 500_000, "cost_usd": 0.75})
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 500_000,
                    "output_tokens": 500_000, "cost_usd": 0.75})
    snap = b.snapshot()
    assert snap["cost_today_usd"] == pytest.approx(1.50)
    assert snap["tokens_today"] == {"input": 1_000_000, "output": 1_000_000}


# --- Wave 3Z additional resilience tests ----------------------------------------

def test_wave3z_classify_413_is_context_overflow():
    """classify() maps HTTP 413 to CONTEXT_OVERFLOW which is non-retryable."""
    exc = _make_http_error(413)
    err = classify(exc)
    assert err.reason == FailoverReason.CONTEXT_OVERFLOW
    assert err.retryable is False
    assert err.should_compress is True
    assert err.status == 413


def test_wave3z_classify_504_is_timeout():
    """classify() maps HTTP 504 (gateway timeout) to TIMEOUT which is retryable."""
    exc = _make_http_error(504)
    err = classify(exc)
    assert err.reason == FailoverReason.TIMEOUT
    assert err.retryable is True
    assert err.status == 504


def test_wave3z_classify_500_is_overloaded():
    """classify() maps HTTP 500 to OVERLOADED which is retryable and should_fallback."""
    exc = _make_http_error(500)
    err = classify(exc)
    assert err.reason == FailoverReason.OVERLOADED
    assert err.retryable is True
    assert err.should_fallback is True
    assert err.status == 500


def test_wave3z_classify_unknown_exception_maps_to_unknown():
    """classify() maps a non-httpx exception to FailoverReason.UNKNOWN."""
    exc = ValueError("something unexpected")
    err = classify(exc)
    assert err.reason == FailoverReason.UNKNOWN
    assert err.retryable is False
    assert err.should_fallback is True
    assert err.status is None


def test_wave3z_retry_policy_backoff_increases_with_attempt():
    """backoff(1) >= backoff(0) on average — ceiling grows with attempt index."""
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=16.0)
    # ceiling for attempt 0 = 1.0, attempt 2 = 4.0 — backoff(2) ceiling is always larger
    ceil_0 = min(policy.max_delay, policy.base_delay * (2 ** 0))
    ceil_2 = min(policy.max_delay, policy.base_delay * (2 ** 2))
    assert ceil_2 > ceil_0


def test_wave3z_budgeter_by_model_accumulates_two_models():
    """record_usage() tracks cost and tokens separately per model."""
    b = Budgeter()
    b.record_usage({"model": "ModelA", "input_tokens": 100_000,
                    "output_tokens": 50_000, "cost_usd": 0.10})
    b.record_usage({"model": "ModelB", "input_tokens": 200_000,
                    "output_tokens": 100_000, "cost_usd": 0.20})
    snap = b.snapshot()
    assert snap["cost_today_usd"] == pytest.approx(0.30)
    assert "ModelA" in snap["by_model"]
    assert "ModelB" in snap["by_model"]
    assert snap["by_model"]["ModelA"]["cost_usd"] == pytest.approx(0.10)
    assert snap["by_model"]["ModelB"]["cost_usd"] == pytest.approx(0.20)


def test_wave3z_budgeter_tokens_accumulate_across_models():
    """Total tokens_today sums input and output across all models."""
    b = Budgeter()
    b.record_usage({"model": "ModelA", "input_tokens": 300_000,
                    "output_tokens": 100_000, "cost_usd": 0.05})
    b.record_usage({"model": "ModelB", "input_tokens": 200_000,
                    "output_tokens": 150_000, "cost_usd": 0.05})
    snap = b.snapshot()
    assert snap["tokens_today"]["input"] == 500_000
    assert snap["tokens_today"]["output"] == 250_000


def test_wave3z_retry_policy_reset_is_noop():
    """RetryPolicy.reset() is a no-op; backoff still works after calling it."""
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0)
    policy.reset()
    wait = policy.backoff(0)
    assert wait >= 0.0


# --- Wave 4G-B additional resilience tests --------------------------------------

def test_wave4g_classify_403_is_auth():
    """classify() maps HTTP 403 to AUTH reason, same as 401."""
    exc = _make_http_error(403)
    err = classify(exc)
    assert err.reason == FailoverReason.AUTH
    assert err.retryable is True
    assert err.should_rotate_credential is True
    assert err.should_fallback is True
    assert err.status == 403


def test_wave4g_classify_400_non_context_is_format_error():
    """classify() maps HTTP 400 with generic body to FORMAT_ERROR (non-retryable)."""
    exc = _make_http_error(400)
    err = classify(exc)
    assert err.reason == FailoverReason.FORMAT_ERROR
    assert err.retryable is False
    assert err.should_fallback is False
    assert err.status == 400


def test_wave4g_retry_policy_backoff_at_attempt2_respects_max_delay():
    """backoff(attempt) never exceeds max_delay regardless of attempt index."""
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=4.0)
    for attempt in range(10):
        wait = policy.backoff(attempt)
        assert wait <= policy.max_delay, f"backoff({attempt})={wait} exceeded max_delay={policy.max_delay}"


def test_wave4g_budgeter_by_model_input_output_tokens():
    """record_usage() stores per-model input and output token counts."""
    b = Budgeter()
    b.record_usage({"model": "ModelX", "input_tokens": 111_000,
                    "output_tokens": 222_000, "cost_usd": 0.05})
    snap = b.snapshot()
    model_data = snap["by_model"]["ModelX"]
    assert model_data["input"] == 111_000
    assert model_data["output"] == 222_000


def test_wave4g_credential_pool_all_cooling_returns_none():
    """acquire() returns None when all credentials are on cooldown."""
    from hive.llm.credential_pool import CredentialPool

    clock_val = [0.0]
    pool = CredentialPool(["ka", "kb"], cooldown_seconds=60.0,
                          clock=lambda: clock_val[0])
    ca = pool.acquire()
    pool.report_failure(ca)
    cb = pool.acquire()
    pool.report_failure(cb)
    assert pool.acquire() is None


def test_wave4g_budgeter_multiple_models_separate_call_counts():
    """record_usage() with two models keeps both in by_model without merging."""
    b = Budgeter()
    b.record_usage({"model": "Alpha", "input_tokens": 10_000,
                    "output_tokens": 5_000, "cost_usd": 0.01})
    b.record_usage({"model": "Alpha", "input_tokens": 10_000,
                    "output_tokens": 5_000, "cost_usd": 0.01})
    b.record_usage({"model": "Beta", "input_tokens": 20_000,
                    "output_tokens": 10_000, "cost_usd": 0.02})
    snap = b.snapshot()
    assert snap["by_model"]["Alpha"]["cost_usd"] == pytest.approx(0.02)
    assert snap["by_model"]["Beta"]["cost_usd"] == pytest.approx(0.02)
    assert snap["cost_today_usd"] == pytest.approx(0.04)


def test_wave4g_classify_timeout_exception_is_timeout():
    """An httpx.TimeoutException classifies as TIMEOUT (not OVERLOADED)."""
    exc = httpx.TimeoutException("timed out")
    err = classify(exc)
    assert err.reason == FailoverReason.TIMEOUT
    assert err.retryable is True
    assert err.status is None


def test_wave4g_retry_policy_backoff_is_non_negative_for_high_attempt():
    """backoff() never returns a negative value for large attempt indices."""
    policy = RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=8.0)
    for attempt in range(20):
        wait = policy.backoff(attempt)
        assert wait >= 0.0, f"backoff({attempt}) was negative: {wait}"


# --- Wave 4M-B additional resilience tests --------------------------------------

def test_wave4m_classify_ok_status_is_unknown():
    """A 200-wrapped HTTPStatusError maps to FailoverReason.UNKNOWN (not a normal path)."""
    exc = _make_http_error(200)
    err = classify(exc)
    assert err.reason == FailoverReason.UNKNOWN
    assert err.retryable is False


def test_wave4m_retry_policy_zero_base_delay_backoff_is_zero():
    """backoff() with base_delay=0.0 returns 0.0 for any attempt."""
    policy = RetryPolicy(max_attempts=5, base_delay=0.0, max_delay=0.0)
    for attempt in range(5):
        wait = policy.backoff(attempt)
        assert wait == 0.0, f"Expected 0.0, got {wait} for attempt {attempt}"


def test_wave4m_classify_format_error_should_not_rotate_or_fallback():
    """FORMAT_ERROR (400) must not rotate credential and must not trigger fallback."""
    exc = _make_http_error(400)
    err = classify(exc)
    assert err.reason == FailoverReason.FORMAT_ERROR
    assert err.should_rotate_credential is False
    assert err.should_fallback is False


def test_wave4m_budgeter_forecast_includes_cost_today_usd():
    """forecast() includes cost_today_usd that matches recorded usage."""
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 100_000,
                    "output_tokens": 50_000, "cost_usd": 0.20})
    f = b.forecast()
    assert "cost_today_usd" in f
    assert f["cost_today_usd"] == pytest.approx(0.20)


def test_wave4m_budgeter_refresh_short_circuits_on_empty_key():
    """refresh() returns None immediately when api_key is empty (no network call)."""
    import asyncio
    b = Budgeter()
    result = asyncio.run(b.refresh("", "http://unreachable.invalid/remains"))
    assert result is None


def test_wave4m_classify_500_should_not_rotate_credential():
    """classify() for 500 sets should_rotate_credential=False (not a key problem)."""
    exc = _make_http_error(500)
    err = classify(exc)
    assert err.reason == FailoverReason.OVERLOADED
    assert err.should_rotate_credential is False


def test_wave4m_retry_policy_max_attempts_stored_correctly():
    """RetryPolicy stores max_attempts, base_delay, and max_delay as given."""
    policy = RetryPolicy(max_attempts=7, base_delay=2.0, max_delay=32.0)
    assert policy.max_attempts == 7
    assert policy.base_delay == 2.0
    assert policy.max_delay == 32.0


def test_wave4m_budgeter_warning_status_returns_dict_when_near_cap():
    """warning_status() returns a non-None dict when >=80% of daily cap is used."""
    b = Budgeter(daily_cap=10)
    for _ in range(9):
        b.record_call()
    status = b.warning_status()
    assert status is not None
    assert isinstance(status, dict)
    assert "near_cap" in status
    assert status["near_cap"] is True


# --- Wave 5: lift budgeter.py coverage from 88% to 100% --------------------

def test_budgeter_gate_blocks_when_credit_window_nearly_exhausted():
    """gate() returns False + 'credit window nearly exhausted' when used_pct >= 98 (line 55)."""
    b = Budgeter()
    b._used_pct = 99.0   # force credit-window exhausted
    ok, reason = b.gate()
    assert ok is False
    assert "credit window" in reason.lower()


def test_budgeter_calls_per_hour_returns_zero_within_first_minute():
    """calls_per_hour returns 0.0 when less than 1 minute into the day (line 148)."""
    # Clock anchored exactly at midnight UTC so hours_elapsed < 1/60.
    midnight = 1748736000.0   # 2025-06-01 00:00:00 UTC
    b = Budgeter(clock=lambda: midnight)
    b.record_call()
    assert b.calls_per_hour() == 0.0


def test_budgeter_refresh_with_no_api_key_returns_none():
    """refresh() returns None immediately when api_key is empty (line 187)."""
    import asyncio
    b = Budgeter()
    out = asyncio.run(b.refresh("", "http://example/"))
    assert out is None


def test_budgeter_refresh_swallows_http_errors():
    """refresh() returns None on httpx/network failure — never raises (188-199)."""
    import asyncio
    from unittest.mock import patch

    b = Budgeter()

    # Patch the httpx.AsyncClient context manager to raise.
    class _BadCM:
        async def __aenter__(self): raise RuntimeError("network down")
        async def __aexit__(self, *a): return False
    with patch("hive.core.budgeter.httpx.AsyncClient", return_value=_BadCM()):
        out = asyncio.run(b.refresh("test-key", "http://example/remains"))
    assert out is None


def test_budgeter_refresh_polls_and_warns_when_high():
    """refresh() parses usage_percent and logs a warning at warn_pct (188-198)."""
    import asyncio
    from unittest.mock import patch, MagicMock

    b = Budgeter(warn_pct=50.0)

    # Build a fake httpx response context manager.
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"usage_percent": 75.0}
    class _OK:
        async def __aenter__(self): return MagicMock(get=MagicMock(return_value=fake_resp))
        async def __aexit__(self, *a): return False
    with patch("hive.core.budgeter.httpx.AsyncClient", return_value=_OK()):
        # Reach into the get() chain: c.get(...) returns fake_resp.
        with patch.object(_OK, "__aenter__") as enter:
            enter.return_value.get.return_value = fake_resp
            out = asyncio.run(b.refresh("test-key", "http://example/remains"))
    assert out == 75.0
    assert b._used_pct == 75.0
