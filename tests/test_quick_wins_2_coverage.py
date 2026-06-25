"""Coverage batch — tools/base, tools/executor, llm/router follow-up.

Target modules:
  src/hive/tools/base.py        95% → 100% (1 line: ToolSpec.__repr__)
  src/hive/tools/executor.py    96% → 100% (5 lines: write-tool bad-path,
                                 execute_approved unknown/unavailable, audit raises)
  src/hive/llm/router.py        93% → 100% (10 lines: planner-enabled branch,
                                 mid-failover rotation after cool, stream
                                 no-credentials, stream astream exception)

All tests are offline — fake adapters, monkey-patched pools, no network.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import httpx
import pytest

from hive.core.types import ToolResult
from hive.llm.adapters.base import CompletionResult, LLMAdapter, Usage
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.file_safety import check_path


# ---------------------------------------------------------------------------
# tools/base.py — ToolSpec.__repr__
# ---------------------------------------------------------------------------

def test_toolspec_repr_includes_name_category_and_dangerous():
    """ToolSpec.__repr__ includes name, category, dangerous flag — line 26."""
    spec = ToolSpec(name="read_file", description="d", dangerous=True,
                    category="filesystem")
    text = repr(spec)
    assert "read_file" in text
    assert "filesystem" in text
    assert "dangerous=True" in text


# ---------------------------------------------------------------------------
# tools/executor.py — path-safety on writes + audit raise + approved paths
# ---------------------------------------------------------------------------

class _Spy(BaseTool):
    """Inline tool for executor tests."""

    def __init__(self, name="spy", dangerous=False, available=True):
        self._spec = ToolSpec(name=name, description="t", dangerous=dangerous)
        self._available = available
        self.ran = False

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, **params) -> ToolResult:
        self.ran = True
        return ToolResult(tool_name=self._spec.name, content="ok")

    def available(self) -> bool:
        return self._available


def test_executor_rejects_unsafe_path_before_gate(tmp_path, caplog):
    """When a write tool has a traversal path, the executor errors at the
    path-safety check (line 136) BEFORE the approval gate is consulted."""
    bad_path = str(tmp_path / "../../../etc/passwd")
    # Sanity: confirm the path really trips check_path.
    assert check_path(bad_path) is not None

    spy = _Spy("write_file")
    gate_calls: list = []

    class _Gate:
        def is_dangerous(self, name, args):
            gate_calls.append((name, args))
            return False
        def request(self, name, args, reason):
            raise AssertionError("gate.request should NOT be reached on unsafe path")

    ex = ToolExecutor({spy.spec.name: spy}, gate=_Gate())
    out = asyncio.run(ex.execute("write_file", {"path": bad_path, "content": "x"}))

    assert out.status is DispatchStatus.ERROR
    err = out.error or ""
    assert "outside" in err.lower() or "traversal" in err.lower()
    assert spy.ran is False, "unsafe-path tool must not run"
    assert gate_calls == [], "unsafe-path must short-circuit BEFORE the gate"


def test_execute_approved_unknown_tool_returns_error():
    """execute_approved with an unregistered tool returns ERROR (line 160)."""
    ex = ToolExecutor({}, audit=lambda e: None)
    out = asyncio.run(ex.execute_approved("nope", {}))
    assert out.status is DispatchStatus.ERROR
    assert out.error is not None and "unknown tool" in out.error


def test_execute_approved_unavailable_tool_returns_error():
    """execute_approved refuses a tool that reports itself unavailable (line 163)."""
    spy = _Spy("offline", available=False)
    ex = ToolExecutor({spy.spec.name: spy})
    out = asyncio.run(ex.execute_approved("offline", {}))
    assert out.status is DispatchStatus.ERROR
    assert out.error is not None and "unavailable" in out.error
    assert spy.ran is False


def test_executor_logs_warning_when_audit_raises(caplog):
    """When the audit sink raises, the executor logs a warning but still
    returns the dispatch (lines 203-204)."""
    spy = _Spy("safe")

    def boom_audit(_event):
        raise RuntimeError("audit down")

    ex = ToolExecutor({spy.spec.name: spy}, audit=boom_audit)
    with caplog.at_level(logging.WARNING, logger="hive.tools.executor"):
        out = asyncio.run(ex.execute("safe", {"x": 1}))

    # The dispatch itself is still returned with the real outcome.
    assert out.status is DispatchStatus.OK
    # And the audit failure was logged at WARNING.
    audit_warnings = [r for r in caplog.records if "audit" in r.getMessage().lower()]
    assert audit_warnings, "expected a 'audit write failed' warning"
    assert any("audit" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# llm/router.py — planner-enabled branch + mid-failover rotation
# ---------------------------------------------------------------------------

def _router_cfg(tmp_path, monkeypatch, **overrides):
    """Build a HiveConfig with the env vars router.complete()/stream() need."""
    monkeypatch.setenv("MINIMAX_API_KEY", "k1")
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    for k, v in overrides.items():
        cfg = replace(cfg, **{k: v})
    return cfg


def test_router_constructs_codex_planner_when_enabled(tmp_path, monkeypatch):
    """When planner is None and cfg.planner_enabled=True, the router builds a
    codex planner (line 99)."""
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter

    cfg = _router_cfg(tmp_path, monkeypatch, planner_enabled=True)
    router = ModelRouter(config=cfg, retry=RetryPolicy(max_attempts=1))
    assert router._planner is not None
    assert callable(router._planner)


def test_router_complete_raises_provider_error_after_rotating_credential_out(tmp_path, monkeypatch):
    """Single-cred pool + rotate-worthy failure + second attempt = pool empty,
    last_ce set → router raises ProviderError chained from the original
    ClassifiedError (lines 159-160)."""
    from hive.llm.adapters.base import CompletionResult, LLMAdapter, Usage
    from hive.llm.credential_pool import CredentialPool
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter, ProviderError
    from hive.core.types import Message, Role

    class _RateLimitAdapter(LLMAdapter):
        """Always raises 429 — rotate-worthy, retryable."""
        name = "rl"

        async def complete(self, request, *, api_key):
            req = httpx.Request("POST", "https://api.example/v1/msg")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("rate limit", request=req, response=resp)

    # Single key: after report_failure() it goes into cooldown; the next
    # acquire() returns None, surfacing the ProviderError with last_ce.
    pool = CredentialPool(["only_key"], cooldown_seconds=10.0)
    router = ModelRouter(
        config=_router_cfg(tmp_path, monkeypatch),
        adapter=_RateLimitAdapter(),
        credential_pool=pool,
        retry=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0),
    )

    with pytest.raises(ProviderError) as ei:
        asyncio.run(router.complete([Message(role=Role.USER, content="hi")]))
    # The reason text must be from the last classified error (rate_limit),
    # not a generic "all cooling" — that's the whole point of 159-160.
    assert "rate_limit" in str(ei.value)


def test_router_complete_raises_no_credentials_when_pool_pre_cooled(tmp_path, monkeypatch):
    """When the pool has creds but ALL are pre-cooled (and no prior failure
    in this call set last_ce), router raises NoCredentialsError("all credentials
    are in cooldown") — line 161. Distinct from the no-creds-configured branch."""
    from hive.llm.adapters.base import CompletionResult, LLMAdapter, Usage
    from hive.llm.credential_pool import CredentialPool
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter, NoCredentialsError
    from hive.core.types import Message, Role

    # Park a single key in cooldown BEFORE complete() runs — so last_ce is
    # never set on this call, exercising the bare "all cooling" message.
    pool = CredentialPool(["k"], clock=lambda: 0.0)
    cred = pool.acquire()
    assert cred is not None
    pool.cooldown(cred, 60.0)  # until t=60

    router = ModelRouter(
        config=_router_cfg(tmp_path, monkeypatch),
        adapter=_StreamingAdapter(["hi"]),
        credential_pool=pool,
        retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
    )

    with pytest.raises(NoCredentialsError, match="all credentials are in cooldown"):
        asyncio.run(router.complete([Message(role=Role.USER, content="hi")]))


# ---------------------------------------------------------------------------
# llm/router.py — stream() error paths
# ---------------------------------------------------------------------------

class _StreamingAdapter(LLMAdapter):
    """Fake adapter whose astream() yields text deltas."""
    name = "stream"

    def __init__(self, deltas):
        self._deltas = list(deltas)

    async def complete(self, request, *, api_key):
        return CompletionResult(text="".join(self._deltas), model=request.model,
                                usage=Usage(input_tokens=1, output_tokens=len(self._deltas)))

    async def astream(self, request, *, api_key):
        for d in self._deltas:
            yield d


class _BoomStreamAdapter(LLMAdapter):
    """Fake adapter whose astream() raises immediately."""
    name = "boom"

    async def complete(self, request, *, api_key):
        return CompletionResult(text="", model=request.model, usage=Usage(0, 0))

    async def astream(self, request, *, api_key):
        if True:  # explicit raise on first iteration
            raise RuntimeError("transport down")
        yield ""  # pragma: no cover - never reached


def test_router_stream_empty_pool_raises_no_credentials(tmp_path, monkeypatch):
    """stream() with no configured creds raises NoCredentialsError (lines 206-207)."""
    from hive.llm.credential_pool import CredentialPool
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter, NoCredentialsError
    from hive.core.types import Message, Role

    monkeypatch.setenv("MINIMAX_API_KEY", "")
    # Build a router with an explicit empty pool so we exercise the
    # `len(self._pool) == 0` branch rather than the "all cooling" branch.
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    router = ModelRouter(config=cfg, adapter=_StreamingAdapter(["hi"]),
                         credential_pool=CredentialPool([]),
                         retry=RetryPolicy(max_attempts=1))

    async def drain():
        async for _ in router.stream([Message(role=Role.USER, content="hi")]):
            pass

    with pytest.raises(NoCredentialsError, match="no API key"):
        asyncio.run(drain())


def test_router_stream_all_cooling_raises_no_credentials(tmp_path, monkeypatch):
    """stream() with creds in cooldown raises NoCredentialsError (line 208)."""
    from hive.llm.credential_pool import CredentialPool
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter, NoCredentialsError
    from hive.core.types import Message, Role

    # Build a pool with one key, parked in cooldown.
    pool = CredentialPool(["k"], clock=lambda: 0.0)
    cred = pool.acquire()
    assert cred is not None
    pool.cooldown(cred, 60.0)  # until t=60

    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    router = ModelRouter(config=cfg, adapter=_StreamingAdapter(["hi"]),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1))

    async def drain():
        async for _ in router.stream([Message(role=Role.USER, content="hi")]):
            pass

    with pytest.raises(NoCredentialsError, match="cooldown"):
        asyncio.run(drain())


def test_router_stream_provider_error_on_astream_exception(tmp_path, monkeypatch):
    """When astream() raises, stream() reports the failure and re-raises as
    ProviderError (lines 220-222)."""
    from hive.llm.credential_pool import CredentialPool
    from hive.llm.failover import RetryPolicy
    from hive.llm.router import ModelRouter, ProviderError
    from hive.core.types import Message, Role

    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    pool = CredentialPool(["k"])
    router = ModelRouter(config=cfg, adapter=_BoomStreamAdapter(),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1))

    async def drain():
        async for _ in router.stream([Message(role=Role.USER, content="hi")]):
            pass

    with pytest.raises(ProviderError, match="stream failed"):
        asyncio.run(drain())
    # The failing credential must have been reported as a failure.
    assert pool.total_failures() >= 1
