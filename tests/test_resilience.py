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
