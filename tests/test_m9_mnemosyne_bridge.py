"""M9-b — Mnemosyne host-LLM async bridge: dedicated event loop + thread.

Tests verify that:
  1. set_host_llm_backend installs a sync callable that the inner provider receives.
  2. The sync callable crosses from a worker thread back to the dedicated async loop
     without touching the main event loop.
  3. The bridge handles inner providers that lack set_host_llm_backend (safe no-op).
  4. runtime.py wires the bridge automatically when the provider is HiveMnemosyneProvider.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.memory.mnemosyne_provider import HiveMnemosyneProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_inner(has_backend: bool = True):
    inner = MagicMock()
    if not has_backend:
        del inner.set_host_llm_backend  # remove so hasattr returns False
    return inner


def _make_adapter(response: str = "consolidated") -> MagicMock:
    adapter = MagicMock()
    from hive.llm.adapters.base import CompletionResult
    adapter.complete = AsyncMock(return_value=CompletionResult(text=response, model="fake"))
    return adapter


# ---------------------------------------------------------------------------
# Unit tests: set_host_llm_backend
# ---------------------------------------------------------------------------

def test_backend_wires_sync_callable_to_inner():
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("result-text")

    provider.set_host_llm_backend(adapter, model="test-model", api_key="k")

    inner.set_host_llm_backend.assert_called_once()
    sync_fn = inner.set_host_llm_backend.call_args[0][0]
    assert callable(sync_fn)


def test_sync_callable_returns_adapter_text():
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("hello from llm")

    provider.set_host_llm_backend(adapter, model="test-model", api_key="k")

    sync_fn = inner.set_host_llm_backend.call_args[0][0]
    result = sync_fn("summarise this memory")
    assert result == "hello from llm"


def test_sync_callable_works_from_worker_thread():
    """The sync callable must be callable from a non-asyncio thread."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("from thread")

    provider.set_host_llm_backend(adapter, model="m", api_key="k")
    sync_fn = inner.set_host_llm_backend.call_args[0][0]

    result_holder: list[str] = []
    exc_holder: list[Exception] = []

    def _worker():
        try:
            result_holder.append(sync_fn("prompt from thread"))
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert not exc_holder, f"thread raised: {exc_holder[0]}"
    assert result_holder == ["from thread"]


def test_sync_callable_does_not_use_running_event_loop():
    """Calling the bridge from inside an asyncio task must not raise 'attached to different loop'."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("async-safe")

    provider.set_host_llm_backend(adapter, model="m", api_key="k")
    sync_fn = inner.set_host_llm_backend.call_args[0][0]

    result_holder: list[str] = []

    async def _task():
        # run_coroutine_threadsafe submits to the DEDICATED loop, not the running one
        result_holder.append(await asyncio.get_event_loop().run_in_executor(
            None, sync_fn, "cross-loop prompt"))

    asyncio.run(_task())
    assert result_holder == ["async-safe"]


def test_no_op_when_inner_lacks_backend():
    """Provider without set_host_llm_backend should not raise."""
    inner = _make_inner(has_backend=False)
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter()

    provider.set_host_llm_backend(adapter, model="m", api_key="k")  # must not raise


# ---------------------------------------------------------------------------
# Integration: runtime.py wires the bridge automatically
# ---------------------------------------------------------------------------

def test_runtime_wires_bridge_for_mnemosyne_provider(tmp_path, monkeypatch):
    """If build_mnemosyne_provider returns a HiveMnemosyneProvider, build() must wire it."""
    from hive.core.config import HiveConfig
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS

    inner = _make_inner()
    fake_provider = HiveMnemosyneProvider(inner)

    monkeypatch.setattr(
        "hive.runtime.build_mnemosyne_provider",
        lambda **_: fake_provider,
    )

    class _ScriptRouter:
        async def complete(self, *a, **kw):
            return CompletionResult(text="ok", model="fake")
        async def aclose(self): pass

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    HiveOS.build(cfg, router=_ScriptRouter())

    inner.set_host_llm_backend.assert_called_once()
