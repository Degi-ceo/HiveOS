"""A3 — Mnemosyne host-LLM bridge (dedicated loop, cross-thread safe)."""
from __future__ import annotations

import threading

import pytest

from hive.llm.adapters.base import CompletionResult, LLMAdapter
from hive.llm.host_bridge import HostLLMBridge


class _FakeAdapter(LLMAdapter):
    name = "fake"

    def __init__(self, text="consolidated", *, fail=False):
        self._text, self._fail = text, fail
        self.calls = 0

    async def complete(self, request, *, api_key):
        self.calls += 1
        if self._fail:
            raise RuntimeError("boom")
        return CompletionResult(text=self._text, model=request.model)


def _bridge(adapter):
    return HostLLMBridge(provider="x", base_url="", api_key="k", model="m", adapter=adapter)


def test_complete_runs_async_adapter_synchronously():
    b = _bridge(_FakeAdapter("hello world"))
    try:
        assert b.complete("prompt") == "hello world"
    finally:
        b.close()


def test_complete_works_from_a_foreign_thread():
    """The whole point: Mnemosyne calls complete() from its own consolidation thread.
    The bridge's dedicated loop must service it without touching any other loop."""
    b = _bridge(_FakeAdapter("from thread"))
    result = {}

    def worker():
        result["text"] = b.complete("p")

    try:
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert result.get("text") == "from thread"
    finally:
        b.close()


def test_no_key_returns_none_without_calling_adapter():
    fake = _FakeAdapter()
    b = HostLLMBridge(provider="x", base_url="", api_key="", model="m", adapter=fake)
    assert b.complete("p") is None
    assert fake.calls == 0
    b.close()


def test_adapter_failure_degrades_to_none():
    b = _bridge(_FakeAdapter(fail=True))
    try:
        assert b.complete("p") is None
    finally:
        b.close()


def test_close_is_safe_when_never_used():
    b = _bridge(_FakeAdapter())
    b.close()  # loop never started -> no-op, no error


# --- registration against the real (installed) Mnemosyne seam ------------------

def test_register_host_llm_round_trip():
    pytest.importorskip("mnemosyne.core.llm_backends",
                        reason="mnemosyne not installed")
    from mnemosyne.core.llm_backends import get_host_llm_backend, set_host_llm_backend
    from hive.memory.mnemosyne_provider import _register_host_llm

    b = _bridge(_FakeAdapter())
    try:
        assert _register_host_llm(b) is True
        assert get_host_llm_backend() is b
        # Mnemosyne's protocol shape: sync complete returning text
        assert get_host_llm_backend().complete("x", max_tokens=8, temperature=0.0,
                                               timeout=10.0) == "consolidated"
    finally:
        set_host_llm_backend(None)   # clean the process-global
        b.close()


# --- runtime wires a bridge ----------------------------------------------------

def test_runtime_has_host_llm_bridge(tmp_path, monkeypatch):
    import asyncio
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS

    class _R:
        async def complete(self, *a, **k): return CompletionResult(text="x", model="m")
        async def aclose(self): pass
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_R())
    assert isinstance(h.host_llm, HostLLMBridge)
    asyncio.run(h.aclose())   # closes the (unused) bridge cleanly
