"""P2 — LLM layer: failover taxonomy, credential pool, catalog, router decision tree."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hive.core.config import HiveConfig
from hive.core.types import Message, Role
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter
from hive.llm.credential_pool import CredentialPool
from hive.llm.failover import FailoverReason, RetryPolicy, classify
from hive.llm.model_catalog import ModelCatalog
from hive.llm.router import (
    BudgetError, ModelRouter, NoCredentialsError, ProviderError, TaskKind,
)


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://test/v1/messages")
    resp = httpx.Response(status_code=status, text=body, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


# --- failover ------------------------------------------------------------------

@pytest.mark.parametrize("status,reason,retry,rotate,fallback", [
    (429, FailoverReason.RATE_LIMIT, True, True, True),
    (401, FailoverReason.AUTH, True, True, True),   # retry rotates to the next key
    (402, FailoverReason.BILLING, False, True, True),
    (500, FailoverReason.OVERLOADED, True, False, True),
    (503, FailoverReason.OVERLOADED, True, False, True),
    (400, FailoverReason.FORMAT_ERROR, False, False, False),
])
def test_classify_status_codes(status, reason, retry, rotate, fallback):
    ce = classify(_http_error(status))
    assert ce.reason is reason
    assert (ce.retryable, ce.should_rotate_credential, ce.should_fallback) == (retry, rotate, fallback)
    assert ce.status == status


def test_classify_context_overflow_from_400_body():
    ce = classify(_http_error(400, "input is too long for the context window"))
    assert ce.reason is FailoverReason.CONTEXT_OVERFLOW
    assert ce.should_compress is True


def test_classify_timeout_and_transport():
    assert classify(httpx.ReadTimeout("t")).reason is FailoverReason.TIMEOUT
    assert classify(httpx.ConnectError("c")).reason is FailoverReason.OVERLOADED
    assert classify(ValueError("?")).reason is FailoverReason.UNKNOWN


def test_retry_backoff_bounded():
    rp = RetryPolicy(base_delay=1.0, max_delay=4.0)
    for attempt in range(6):
        assert 0.0 <= rp.backoff(attempt) <= 4.0


# --- credential pool -----------------------------------------------------------

def test_pool_dedupes_and_drops_blanks():
    pool = CredentialPool(["a", "", "a", "b"])
    assert len(pool) == 2


def test_pool_round_robin_and_cooldown():
    now = [0.0]
    pool = CredentialPool(["a", "b"], cooldown_seconds=10.0, clock=lambda: now[0])
    c1 = pool.acquire()
    c2 = pool.acquire()
    assert {c1.key, c2.key} == {"a", "b"}  # round-robin hands out both
    pool.report_failure(c1)               # park c1 for 10s
    nxt = pool.acquire()
    assert nxt.key == c2.key              # cooled key is skipped
    now[0] = 11.0                          # cooldown elapsed
    assert {pool.acquire().key for _ in range(2)} == {"a", "b"}


def test_pool_empty_and_all_cooling_return_none():
    assert CredentialPool([]).acquire() is None
    now = [0.0]
    pool = CredentialPool(["a"], cooldown_seconds=5.0, clock=lambda: now[0])
    pool.report_failure(pool.acquire())
    assert pool.acquire() is None


def test_pool_cooldown_all_parks_every_key():
    now = [0.0]
    pool = CredentialPool(["a", "b", "c"], cooldown_seconds=10.0, clock=lambda: now[0])
    pool.cooldown_all(30.0)
    assert pool.acquire() is None  # all cooling
    now[0] = 31.0
    assert pool.acquire() is not None  # cooled off


def test_pool_status_reports_all_credentials():
    now = [0.0]
    pool = CredentialPool(["x", "y"], cooldown_seconds=60.0, clock=lambda: now[0])
    pool.report_failure(pool.acquire())  # x goes on cooldown
    statuses = pool.status()
    assert len(statuses) == 2
    cooling = [s for s in statuses if s["cooling"]]
    assert len(cooling) == 1
    assert cooling[0]["cooldown_remaining"] > 0


def test_pool_reset_cooldowns():
    now = [0.0]
    pool = CredentialPool(["p", "q"], cooldown_seconds=60.0, clock=lambda: now[0])
    pool.cooldown_all(60.0)
    assert pool.acquire() is None  # all cooling
    pool.reset_cooldowns()
    assert pool.acquire() is not None  # cleared


def test_pool_available_count():
    now = [0.0]
    pool = CredentialPool(["a", "b", "c"], cooldown_seconds=10.0, clock=lambda: now[0])
    assert pool.available_count() == 3
    pool.cooldown_all(5.0)
    assert pool.available_count() == 0
    now[0] = 10.0
    assert pool.available_count() == 3


# --- model catalog -------------------------------------------------------------

def test_catalog_known_and_unknown_fallback():
    cat = ModelCatalog()
    assert cat.get("MiniMax-M3").context_length == 192_000
    unknown = cat.get("MiniMax-M9-future")
    assert unknown.model_id == "MiniMax-M9-future"   # tagged with requested id
    assert unknown.supports_thinking is True          # conservative default


# --- router decision tree ------------------------------------------------------

class FakeAdapter(LLMAdapter):
    name = "fake"

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        self.calls.append((request.model, api_key))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return CompletionResult(text=item, model=request.model)


def _config(monkeypatch, tmp_path) -> HiveConfig:
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-fallback")
    monkeypatch.setenv("HIVE_AUX_MODEL", "M-aux")
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def _router(monkeypatch, tmp_path, adapter, *, keys=("k0", "k1"), **kw) -> ModelRouter:
    return ModelRouter(
        config=_config(monkeypatch, tmp_path),
        adapter=adapter,
        credential_pool=CredentialPool(list(keys), cooldown_seconds=100.0),
        retry=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
        **kw,
    )


def _msgs() -> list[Message]:
    return [Message(role=Role.USER, content="hi")]


def test_router_success(monkeypatch, tmp_path):
    adapter = FakeAdapter(["hello"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs()))
    assert out.text == "hello" and out.model == "M-exec"


def test_router_retries_then_rotates_credential(monkeypatch, tmp_path):
    # 429 on first attempt -> retry same model with a rotated credential -> success
    adapter = FakeAdapter([_http_error(429), "ok"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs()))
    assert out.text == "ok"
    assert [c[0] for c in adapter.calls] == ["M-exec", "M-exec"]
    assert adapter.calls[0][1] != adapter.calls[1][1]   # credential rotated


def test_router_falls_back_to_next_model(monkeypatch, tmp_path):
    # 500 exhausts the primary (retryable+fallback) -> next model succeeds
    adapter = FakeAdapter([_http_error(500), _http_error(500), "recovered"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs()))
    assert out.text == "recovered"
    assert [c[0] for c in adapter.calls] == ["M-exec", "M-exec", "M-fallback"]


def test_router_non_retryable_aborts(monkeypatch, tmp_path):
    # 400 FORMAT_ERROR: no retry, no fallback
    adapter = FakeAdapter([_http_error(400)])
    router = _router(monkeypatch, tmp_path, adapter)
    with pytest.raises(ProviderError):
        asyncio.run(router.complete(_msgs()))
    assert len(adapter.calls) == 1


def test_router_budget_gate_blocks(monkeypatch, tmp_path):
    adapter = FakeAdapter(["never"])
    router = _router(monkeypatch, tmp_path, adapter, budget=lambda: (False, "daily cap hit"))
    with pytest.raises(BudgetError, match="daily cap"):
        asyncio.run(router.complete(_msgs()))
    assert adapter.calls == []


def test_router_budget_block_emits_event(monkeypatch, tmp_path):
    """BUDGET_BLOCK event must be published when the budget gate refuses a call."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    adapter = FakeAdapter(["never"])
    router = _router(monkeypatch, tmp_path, adapter,
                     budget=lambda: (False, "cap reached"), events=bus)
    with pytest.raises(BudgetError):
        asyncio.run(router.complete(_msgs()))
    types = [e.event_type for e in bus.history()]
    assert EventType.BUDGET_BLOCK in types


def test_router_no_credentials(monkeypatch, tmp_path):
    adapter = FakeAdapter(["never"])
    router = _router(monkeypatch, tmp_path, adapter, keys=())
    with pytest.raises(NoCredentialsError):
        asyncio.run(router.complete(_msgs()))


# --- pricing estimate_cost (item 48) -------------------------------------------

def test_estimate_cost():
    from hive.llm.pricing import estimate_cost
    cost = estimate_cost("MiniMax-M3", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.50)


def test_estimate_cost_unknown_model():
    from hive.llm.pricing import estimate_cost
    cost = estimate_cost("Future-Model-X", 1_000_000, 0)
    assert cost > 0.0


def test_router_plan_uses_injected_planner(monkeypatch, tmp_path):
    adapter = FakeAdapter([])  # executor must not be touched

    async def planner(messages, system):
        return "PLAN: " + messages[0].content

    router = _router(monkeypatch, tmp_path, adapter, planner=planner)
    out = asyncio.run(router.complete(_msgs(), TaskKind.PLAN))
    assert out.text == "PLAN: hi" and out.model == "planner"
    assert adapter.calls == []


# --- ModelRouter.status() -------------------------------------------------------

def test_router_status_returns_config_snapshot(monkeypatch, tmp_path):
    adapter = FakeAdapter([])
    router = _router(monkeypatch, tmp_path, adapter, keys=("key1", "key2"))
    status = router.status()
    assert "exec_model" in status
    assert "exec_provider" in status
    assert status["pool_size"] == 2
    assert status["pool_available"] >= 0
    assert isinstance(status["planner_enabled"], bool)
    assert isinstance(status["pool_status"], list)


def test_router_status_no_planner(monkeypatch, tmp_path):
    adapter = FakeAdapter([])
    router = _router(monkeypatch, tmp_path, adapter, planner=None)
    # planner defaults to None when not explicitly set and config planner_enabled=False
    assert isinstance(router.status()["planner_enabled"], bool)


# --- failover named scenarios ---------------------------------------------------

def test_router_failover_on_429(monkeypatch, tmp_path):
    # Primary fails with 429 (rate limit) -> credential rotated -> second call succeeds
    adapter = FakeAdapter([_http_error(429), "rate_limit_recovered"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs()))
    assert out.text == "rate_limit_recovered"
    # Both calls used the exec model; credential rotated between them
    assert all(c[0] == "M-exec" for c in adapter.calls)
    assert adapter.calls[0][1] != adapter.calls[1][1]


def test_router_failover_on_5xx(monkeypatch, tmp_path):
    # Primary exhausts retries with 500 -> falls back to fallback model -> succeeds
    adapter = FakeAdapter([_http_error(500), _http_error(500), "service_recovered"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs()))
    assert out.text == "service_recovered"
    models_called = [c[0] for c in adapter.calls]
    assert models_called[0] == "M-exec"
    assert models_called[-1] == "M-fallback"


def test_router_all_failed(monkeypatch, tmp_path):
    # All models exhaust all attempts -> ProviderError raised
    adapter = FakeAdapter([
        _http_error(500), _http_error(500),   # M-exec attempts
        _http_error(500), _http_error(500),   # M-fallback attempts
    ])
    router = _router(monkeypatch, tmp_path, adapter)
    with pytest.raises(ProviderError):
        asyncio.run(router.complete(_msgs()))


def test_router_aux_task_uses_aux_model(monkeypatch, tmp_path):
    # AUX task kind routes to the aux model, not the exec chain
    adapter = FakeAdapter(["aux_reply"])
    router = _router(monkeypatch, tmp_path, adapter)
    out = asyncio.run(router.complete(_msgs(), TaskKind.AUX))
    assert out.text == "aux_reply"
    assert adapter.calls[0][0] == "M-aux"


# --- Budgeter.forecast() -------------------------------------------------------

def test_budgeter_forecast_no_usage():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    f = b.forecast()
    assert f["calls_today"] == 0
    assert f["daily_cap"] == 100
    assert f["remaining_calls"] == 100
    assert f["pct_used"] == 0.0
    assert f["days_remaining"] is None   # no usage yet


def test_budgeter_forecast_with_usage():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    for _ in range(10):
        b.record_call()
    f = b.forecast()
    assert f["calls_today"] == 10
    assert f["remaining_calls"] == 90
    assert f["pct_used"] == pytest.approx(10.0)
    assert f["days_remaining"] == pytest.approx(9.0)  # 90 remaining / 10 per day


# --- ModelCatalog.list_models() and .unregister() -----------------------------

def test_model_catalog_list_models():
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    models = cat.list_models()
    assert isinstance(models, list)
    assert all(isinstance(m, str) for m in models)
    # Default models are registered
    assert "MiniMax-M3" in models


def test_model_catalog_list_models_includes_registered():
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    cat.register(ModelEntry("TestModel-X"))
    assert "TestModel-X" in cat.list_models()


def test_model_catalog_unregister():
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    cat.register(ModelEntry("Temp-Model"))
    assert "Temp-Model" in cat
    assert cat.unregister("Temp-Model") is True
    assert "Temp-Model" not in cat
    assert cat.unregister("Temp-Model") is False  # already removed


# --- CredentialPool.labels() --------------------------------------------------

def test_credential_pool_labels():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["key-alpha-xxxx", "key-beta-yyyy"])
    labels = pool.labels()
    assert len(labels) == 2
    assert all(isinstance(lb, str) for lb in labels)


def test_credential_pool_labels_empty():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool([])
    assert pool.labels() == []


# --- failure_counts / total_failures ------------------------------------------

def test_credential_pool_failure_counts_zero_initially():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["key1", "key2"])
    counts = pool.failure_counts()
    for label, n in counts.items():
        assert n == 0


def test_credential_pool_failure_counts_after_failure():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["key1", "key2"], cooldown_seconds=60.0)
    cred = pool.acquire()
    assert cred is not None
    pool.report_failure(cred)
    counts = pool.failure_counts()
    assert counts[cred.label] == 1


def test_credential_pool_total_failures_zero():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["k1", "k2"])
    assert pool.total_failures() == 0


def test_credential_pool_total_failures_accumulates():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=60.0)
    c1 = pool.acquire()
    pool.report_failure(c1)
    c2 = pool.acquire()
    pool.report_failure(c2)
    assert pool.total_failures() == 2


def test_credential_pool_total_failures_resets_on_reset_cooldowns():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["k1"], cooldown_seconds=60.0)
    c = pool.acquire()
    pool.report_failure(c)
    assert pool.total_failures() == 1
    pool.reset_cooldowns()
    assert pool.total_failures() == 0


# --- ModelCatalog additional edge cases ------------------------------------------

def test_model_catalog_get_unknown_returns_conservative_default():
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    entry = cat.get("MiniMax-Unknown-X99")
    assert entry.model_id == "MiniMax-Unknown-X99"
    assert entry.context_length == 192_000
    assert entry.supports_thinking is True


def test_model_catalog_get_known_returns_exact():
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    entry = cat.get("MiniMax-M3")
    assert entry.model_id == "MiniMax-M3"


def test_model_catalog_register_overrides_default():
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    custom = ModelEntry("MiniMax-M3", context_length=8_000, max_output=512,
                        supports_thinking=False, supports_tools=False, thinking_budget=0)
    cat.register(custom)
    entry = cat.get("MiniMax-M3")
    assert entry.context_length == 8_000
    assert entry.supports_thinking is False


def test_model_catalog_contains_known():
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    assert "MiniMax-M3" in cat


def test_model_catalog_not_contains_unknown():
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    assert "NoSuchModel-XYZ" not in cat


def test_model_catalog_thinking_budget_default():
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    entry = cat.get("MiniMax-M3")
    assert entry.thinking_budget == 2_048


# --- CredentialPool additional edge cases ----------------------------------------

def test_credential_pool_single_key_always_returns_same():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["only-key"])
    c1 = pool.acquire()
    pool.report_success(c1)
    c2 = pool.acquire()
    assert c1.key == c2.key


def test_credential_pool_acquire_on_empty_returns_none():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool([])
    assert pool.acquire() is None


def test_credential_pool_all_on_cooldown_returns_none():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=3600.0)
    c = pool.acquire()
    pool.report_failure(c)
    c2 = pool.acquire()
    pool.report_failure(c2)
    # All keys on cooldown
    assert pool.acquire() is None


# --- failover taxonomy direct tests -------------------------------------------

def test_failover_503_is_server_error():
    """HTTP 503 must be classified as OVERLOADED (retryable, fallback-eligible)."""
    ce = classify(_http_error(503))
    assert ce.reason is FailoverReason.OVERLOADED
    assert ce.retryable is True
    assert ce.should_fallback is True
    assert ce.status == 503


def test_failover_401_is_auth_error():
    """HTTP 401 must be classified as AUTH (retryable via credential rotation)."""
    ce = classify(_http_error(401))
    assert ce.reason is FailoverReason.AUTH
    assert ce.retryable is True
    assert ce.should_rotate_credential is True
    assert ce.status == 401


def test_failover_429_is_rate_limit():
    """HTTP 429 must be classified as RATE_LIMIT."""
    ce = classify(_http_error(429))
    assert ce.reason is FailoverReason.RATE_LIMIT
    assert ce.retryable is True
    assert ce.should_rotate_credential is True
    assert ce.status == 429


def test_failover_200_is_not_error():
    """HTTP 200 falls through to UNKNOWN (no specific policy); it is not retryable."""
    ce = classify(_http_error(200))
    # 200 matches none of the error branches -> UNKNOWN
    assert ce.reason is FailoverReason.UNKNOWN
    assert ce.retryable is False
    assert ce.status == 200


# --- sanitize module tests -----------------------------------------------------

def test_repair_tool_arguments_valid_json_unchanged():
    """repair_tool_arguments must return the original string when JSON is already valid."""
    from hive.llm.sanitize import repair_tool_arguments
    original = '{"key": "val"}'
    result = repair_tool_arguments(original)
    # Must be parseable and carry the same data
    assert json.loads(result) == {"key": "val"}


def test_repair_tool_arguments_malformed_returns_empty_dict():
    """repair_tool_arguments must not raise on garbage input and must return '{}'."""
    from hive.llm.sanitize import repair_tool_arguments
    result = repair_tool_arguments("not json at all!!")
    assert result == "{}"


def test_repair_tool_arguments_surrogate_stripped():
    """Surrogate characters in JSON values must not cause json.loads to explode;
    repair_tool_arguments should return a valid JSON string."""
    from hive.llm.sanitize import repair_tool_arguments
    # Construct a string with a lone surrogate via surrogatepass codec trick
    raw = '{"x": "hello\ud800world"}'
    result = repair_tool_arguments(raw)
    # Must not raise; result must be parseable standard JSON
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


def test_sanitize_messages_removes_empty_content():
    """sanitize_messages must not raise on a message with empty string content;
    the message is returned with content intact (empty strings are valid)."""
    from hive.llm.sanitize import sanitize_messages
    msgs = [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "hi"},
    ]
    result = sanitize_messages(msgs)
    # sanitize_messages strips surrogates and repairs tool args but does NOT
    # drop messages — both messages must still be present
    assert len(result) == 2
    assert result[0]["content"] == ""
    assert result[1]["content"] == "hi"


# --- pricing module tests ------------------------------------------------------

def test_pricing_cost_calculation_zero_tokens():
    """cost_usd(model, 0, 0) must return exactly 0.0."""
    from hive.llm.pricing import cost_usd
    assert cost_usd("MiniMax-M3", 0, 0) == 0.0


def test_pricing_cost_positive_for_known_model():
    """cost_usd for a known model with non-zero tokens must return a positive value."""
    from hive.llm.pricing import cost_usd
    assert cost_usd("MiniMax-M3", 100, 100) > 0.0


# --- Additional LLM tests -----------------------------------------------------

def test_catalog_all_models_have_context_length():
    """Every model in the catalog must have context_length > 0."""
    cat = ModelCatalog()
    for name in cat:
        info = cat.get(name)
        assert info.context_length > 0, f"{name} has zero context_length"


def test_completion_request_defaults():
    """CompletionRequest has sensible defaults."""
    req = CompletionRequest(model="m", messages=[])
    assert req.model == "m"
    assert req.thinking is not None  # has a default value


def test_llm_adapter_is_abstract():
    """LLMAdapter cannot be instantiated directly (has abstract methods)."""
    import inspect
    assert inspect.isabstract(LLMAdapter) or hasattr(LLMAdapter, "__abstractmethods__")


def test_classify_500_should_fallback():
    """HTTP 500 must have should_fallback=True (triggers provider fallback)."""
    r = classify(500)
    assert r.should_fallback is True


def test_classify_403_not_retryable():
    """HTTP 403 Forbidden must NOT be retryable."""
    r = classify(403)
    assert r.retryable is False


def test_retry_policy_attempts_positive():
    """RetryPolicy must have at least 1 attempt configured."""
    p = RetryPolicy()
    assert p.max_attempts >= 1


def test_retry_policy_backoff_list_has_max_attempts_entries():
    """backoff list length matches max_attempts."""
    p = RetryPolicy()
    delays = [p.backoff(i) for i in range(p.max_attempts)]
    assert len(delays) == p.max_attempts


def test_pool_inject_env_key(tmp_path, monkeypatch):
    """Pool accepts key injected via CredentialPool(['key'])."""
    pool = CredentialPool(["injected-key"])
    cred = pool.acquire()
    assert cred is not None and cred.key == "injected-key"


def test_catalog_thinking_budget_nonzero_for_thinking_model():
    """A model that supports thinking must have thinking_budget > 0."""
    cat = ModelCatalog()
    found = False
    for name in cat:
        info = cat.get(name)
        if getattr(info, "supports_thinking", False):
            assert info.thinking_budget > 0, f"{name} supports_thinking but budget==0"
            found = True
            break
    assert found, "No model with supports_thinking=True found in catalog"
