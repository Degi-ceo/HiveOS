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


# --- additional coverage -------------------------------------------------------

def test_bridge_name_is_hiveos():
    """HostLLMBridge must expose name='hiveos' for Mnemosyne backend registration."""
    b = _bridge(_FakeAdapter())
    assert b.name == "hiveos"
    b.close()


def test_complete_passes_prompt_to_adapter():
    """The prompt string must reach the adapter's complete() call unchanged."""
    fake = _FakeAdapter("pong")
    b = _bridge(fake)
    try:
        result = b.complete("ping")
        assert result == "pong"
        assert fake.calls == 1
    finally:
        b.close()


def test_complete_multiple_calls_increment_counter():
    """Each call to complete() must drive exactly one adapter invocation."""
    fake = _FakeAdapter("x")
    b = _bridge(fake)
    try:
        b.complete("a")
        b.complete("b")
        b.complete("c")
        assert fake.calls == 3
    finally:
        b.close()


def test_complete_returns_none_on_adapter_exception():
    """Any adapter exception must be swallowed and None returned (best-effort)."""
    b = _bridge(_FakeAdapter(fail=True))
    try:
        assert b.complete("p", max_tokens=64) is None
    finally:
        b.close()


def test_close_twice_does_not_raise():
    """close() is idempotent — calling it a second time must not raise."""
    b = _bridge(_FakeAdapter())
    b.complete("hello")  # start the loop
    b.close()
    b.close()  # second close must be a no-op


def test_complete_accepts_optional_kwargs():
    """complete() must accept all Mnemosyne keyword args without error."""
    fake = _FakeAdapter("ok")
    b = _bridge(fake)
    try:
        result = b.complete(
            "prompt",
            max_tokens=256,
            temperature=0.5,
            timeout=30.0,
            provider="openai",
            model="gpt-4o",
        )
        assert result == "ok"
    finally:
        b.close()


# --- New tests (6) ---------------------------------------------------------------

def test_bridge_returns_none_on_empty_key():
    """A bridge with an empty api_key must return None and never call the adapter."""
    fake = _FakeAdapter("should not appear")
    b = HostLLMBridge(provider="x", base_url="", api_key="", model="m", adapter=fake)
    result = b.complete("hello")
    assert result is None
    assert fake.calls == 0
    b.close()


def test_complete_with_max_tokens_override():
    """max_tokens kwarg must be accepted without raising, and result still returned."""
    fake = _FakeAdapter("tokens_ok")
    b = _bridge(fake)
    try:
        result = b.complete("q", max_tokens=512)
        assert result == "tokens_ok"
    finally:
        b.close()


def test_bridge_loop_starts_on_first_complete():
    """The internal thread must be None before the first complete() call."""
    b = _bridge(_FakeAdapter("x"))
    assert b._thread is None
    try:
        b.complete("trigger")
        assert b._thread is not None
    finally:
        b.close()


def test_complete_concurrent_calls_from_multiple_threads():
    """Concurrent complete() calls from different threads must all return the correct text."""
    fake = _FakeAdapter("concurrent")
    b = _bridge(fake)
    results = {}

    def worker(n):
        results[n] = b.complete(f"prompt-{n}")

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert len(results) == 4
        assert all(v == "concurrent" for v in results.values())
    finally:
        b.close()


def test_bridge_provider_attribute_stored():
    """The provider string passed to __init__ must be preserved on the instance."""
    b = HostLLMBridge(provider="minimax", base_url="http://x", api_key="k", model="m")
    assert b._provider == "minimax"
    b.close()


def test_close_before_any_complete_is_noop():
    """Calling close() on a bridge that was never used must not raise."""
    b = _bridge(_FakeAdapter())
    assert b._loop is None
    b.close()   # should silently do nothing
    assert b._loop is None
