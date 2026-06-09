"""P2 — LLM layer: failover taxonomy, credential pool, catalog, router decision tree."""
from __future__ import annotations

import asyncio

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


def test_router_no_credentials(monkeypatch, tmp_path):
    adapter = FakeAdapter(["never"])
    router = _router(monkeypatch, tmp_path, adapter, keys=())
    with pytest.raises(NoCredentialsError):
        asyncio.run(router.complete(_msgs()))


def test_router_plan_uses_injected_planner(monkeypatch, tmp_path):
    adapter = FakeAdapter([])  # executor must not be touched

    async def planner(messages, system):
        return "PLAN: " + messages[0].content

    router = _router(monkeypatch, tmp_path, adapter, planner=planner)
    out = asyncio.run(router.complete(_msgs(), TaskKind.PLAN))
    assert out.text == "PLAN: hi" and out.model == "planner"
    assert adapter.calls == []
