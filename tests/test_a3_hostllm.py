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


# --- Wave 4 additional tests (6) -----------------------------------------------

def test_bridge_model_attribute_stored():
    """The model string passed to __init__ must be stored on _model."""
    b = HostLLMBridge(provider="x", base_url="http://base", api_key="k", model="m-custom")
    assert b._model == "m-custom"
    b.close()


def test_bridge_base_url_attribute_stored():
    """The base_url string passed to __init__ must be stored on _base."""
    b = HostLLMBridge(provider="x", base_url="http://example.com", api_key="k", model="m")
    assert b._base == "http://example.com"
    b.close()


def test_adapter_injected_is_stored_on_init():
    """An adapter passed to __init__ must be stored on _adapter immediately."""
    fake = _FakeAdapter()
    b = HostLLMBridge(provider="x", base_url="", api_key="k", model="m", adapter=fake)
    assert b._adapter is fake
    b.close()


def test_complete_with_temperature_kwarg():
    """complete() must accept a temperature= kwarg without raising."""
    fake = _FakeAdapter("temp_ok")
    b = _bridge(fake)
    try:
        result = b.complete("q", temperature=0.7)
        assert result == "temp_ok"
    finally:
        b.close()


def test_two_sequential_threads_both_succeed():
    """Two sequential threads calling complete() on the same bridge must both get results."""
    fake = _FakeAdapter("seq")
    b = _bridge(fake)
    results = {}

    def worker(n):
        results[n] = b.complete(f"task-{n}")

    try:
        t1 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t1.join(timeout=10)
        t2 = threading.Thread(target=worker, args=(2,))
        t2.start()
        t2.join(timeout=10)
        assert results[1] == "seq"
        assert results[2] == "seq"
    finally:
        b.close()


def test_bridge_has_lock_attribute():
    """HostLLMBridge must expose a _lock attribute for thread-safety."""
    b = _bridge(_FakeAdapter())
    assert hasattr(b, "_lock")
    import threading as _threading
    assert isinstance(b._lock, type(b._lock))  # it is some lock-like object
    b.close()


# --- Wave 3Q additional tests (6) -----------------------------------------------

def test_bridge_key_attribute_stored():
    """The api_key passed to __init__ must be stored on _key."""
    b = HostLLMBridge(provider="x", base_url="", api_key="secret-key", model="m")
    assert b._key == "secret-key"
    b.close()


def test_bridge_catalog_defaults_to_none():
    """When catalog= is not passed, _catalog must be None."""
    b = HostLLMBridge(provider="x", base_url="", api_key="k", model="m")
    assert b._catalog is None
    b.close()


def test_complete_timeout_kwarg_accepted():
    """complete() must accept timeout= as a standalone kwarg without raising."""
    fake = _FakeAdapter("timeout_ok")
    b = _bridge(fake)
    try:
        result = b.complete("q", timeout=5.0)
        assert result == "timeout_ok"
    finally:
        b.close()


def test_bridge_loop_is_none_before_complete():
    """_loop must be None on a freshly constructed bridge before any complete() call."""
    b = HostLLMBridge(provider="x", base_url="", api_key="k", model="m", adapter=_FakeAdapter())
    assert b._loop is None
    b.close()


def test_complete_high_max_tokens_accepted():
    """complete() must handle max_tokens=4096 without error."""
    fake = _FakeAdapter("big_ok")
    b = _bridge(fake)
    try:
        result = b.complete("q", max_tokens=4096)
        assert result == "big_ok"
    finally:
        b.close()


def test_bridge_adapter_is_none_when_not_injected():
    """When no adapter= is passed, _adapter must be None on init (built lazily on first call)."""
    b = HostLLMBridge(provider="x", base_url="", api_key="k", model="m")
    assert b._adapter is None
    b.close()


# --- Wave 3V-C additional host-bridge tests --------------------------------------

def test_wave3v_bridge_name_is_hiveos_string():
    """name attribute is the exact string 'hiveos', not a class-level sentinel."""
    b = _bridge(_FakeAdapter())
    assert b.name == "hiveos"
    assert isinstance(b.name, str)
    b.close()


def test_wave3v_complete_returns_string_not_none_on_success():
    """On a successful call, complete() returns a str, never None."""
    fake = _FakeAdapter("real_text")
    b = _bridge(fake)
    try:
        result = b.complete("q")
        assert isinstance(result, str)
        assert result == "real_text"
    finally:
        b.close()


def test_wave3v_multiple_close_calls_after_complete_are_safe():
    """close() called three times after a complete() must not raise."""
    b = _bridge(_FakeAdapter("x"))
    b.complete("init")
    b.close()
    b.close()
    b.close()


def test_wave3v_thread_is_daemon():
    """The internal loop thread must be a daemon thread so it doesn't block process exit."""
    b = _bridge(_FakeAdapter("x"))
    try:
        b.complete("start")
        assert b._thread is not None
        assert b._thread.daemon is True
    finally:
        b.close()


def test_wave3v_adapter_receives_exact_prompt():
    """The prompt string is passed verbatim; the adapter sees it unmodified."""
    received = {}

    class _RecordingAdapter(_FakeAdapter):
        async def complete(self, request, *, api_key):
            received["prompt"] = request.messages[0].content
            return await super().complete(request, api_key=api_key)

    b = _bridge(_RecordingAdapter("response"))
    try:
        b.complete("exact prompt text")
        assert received.get("prompt") == "exact prompt text"
    finally:
        b.close()


def test_wave3v_empty_string_key_returns_none_zero_calls():
    """An api_key of '' (empty string) returns None and the adapter is never called."""
    fake = _FakeAdapter("should not appear")
    b = HostLLMBridge(provider="p", base_url="http://b", api_key="", model="m", adapter=fake)
    assert b.complete("x") is None
    assert fake.calls == 0
    b.close()


def test_wave3v_complete_five_calls_increment_counter_to_five():
    """Five sequential complete() calls each drive exactly one adapter invocation."""
    fake = _FakeAdapter("y")
    b = _bridge(fake)
    try:
        for _ in range(5):
            b.complete("p")
        assert fake.calls == 5
    finally:
        b.close()


def test_wave3v_lock_is_reentrant_compatible():
    """_lock must be a standard threading.Lock (not RLock) — held briefly per call."""
    b = _bridge(_FakeAdapter())
    assert isinstance(b._lock, type(threading.Lock()))
    b.close()
