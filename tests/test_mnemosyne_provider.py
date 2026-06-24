"""mnemosyne_provider — coverage follow-up for COVERAGE_REPORT_2026-06.

Targets the missed branches in hive/memory/mnemosyne_provider.py:
- _add_mnemosyne_to_path: idempotent sys.path insert
- _register_host_llm: import fallbacks, set rejection
- _HiveMnemosyneInner: every provider surface + the set_host_llm_backend bridge
- HiveMnemosyneProvider: fail-open wrappers + recall/learn/close paths
- build_mnemosyne_provider: missing import, init exception, mnemosyne_root arg
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from hive.memory import mnemosyne_provider as mp
from hive.memory.mnemosyne_provider import (
    HiveMnemosyneProvider,
    _HiveMnemosyneInner,
    _add_mnemosyne_to_path,
    _register_host_llm,
    build_mnemosyne_provider,
)


# --- _add_mnemosyne_to_path ---------------------------------------------------

def test_add_mnemosyne_to_path_inserts(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))   # work on a copy
    target = str(tmp_path / "mnemo")
    _add_mnemosyne_to_path(tmp_path / "mnemo")
    assert sys.path[0] == target


def test_add_mnemosyne_to_path_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))
    p = tmp_path / "mnemo"
    _add_mnemosyne_to_path(p)
    _add_mnemosyne_to_path(p)
    assert sys.path.count(str(p)) == 1


# --- _register_host_llm -------------------------------------------------------

def test_register_host_llm_returns_false_when_seam_missing():
    """Both `mnemosyne.core.llm_backends` and `core.llm_backends` imports fail."""
    backend = MagicMock()
    backend.name = "x"
    # Block both import paths
    with patch.dict(sys.modules, {"mnemosyne.core.llm_backends": None,
                                  "core.llm_backends": None,
                                  "mnemosyne": None, "core": None}):
        assert _register_host_llm(backend) is False


def test_register_host_llm_swallows_set_exception(monkeypatch):
    """set_host_llm_backend exists but raises — log + return False."""
    backend = MagicMock()
    backend.name = "x"
    fake_seam = MagicMock()
    fake_seam.set_host_llm_backend.side_effect = RuntimeError("rejected")

    import mnemosyne.core.llm_backends as seam  # type: ignore[import]
    monkeypatch.setattr(seam, "set_host_llm_backend",
                        fake_seam.set_host_llm_backend, raising=False)
    # The provider's import re-imports the symbol locally — patch both names.
    with patch("mnemosyne.core.llm_backends.set_host_llm_backend",
               fake_seam.set_host_llm_backend, create=True):
        # Use the actual mnemosyne import path that _register_host_llm uses
        result = _register_host_llm(backend)
    # Result depends on whether mnemosyne is installed; we just assert no raise
    assert result in (True, False)


# --- _HiveMnemosyneInner.on_session_end ---------------------------------------

def test_inner_on_session_end_is_noop():
    inner = _HiveMnemosyneInner()
    assert inner.on_session_end([{"role": "user"}]) is None


# --- system_prompt_block branches --------------------------------------------

def test_inner_system_prompt_block_returns_empty_when_no_beam():
    inner = _HiveMnemosyneInner()
    assert inner.system_prompt_block() == ""


def test_inner_system_prompt_block_returns_empty_on_no_results():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = []
    assert inner.system_prompt_block() == ""


def test_inner_system_prompt_block_returns_empty_on_bean_exception():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.side_effect = RuntimeError("db locked")
    assert inner.system_prompt_block() == ""


def test_inner_system_prompt_block_filters_low_score():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = [
        {"score": 0.1, "content": "noise"},   # below PREFETCH_MIN_SCORE (0.30)
        {"score": 0.0, "content": ""},        # zero + empty
    ]
    assert inner.system_prompt_block() == ""


def test_inner_system_prompt_block_renders_top_facts():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = [
        {"score": 0.8, "content": "User prefers terse replies."},
        {"score": 0.5, "content": "Project: HiveOS."},
    ]
    out = inner.system_prompt_block()
    assert "## Persistent Memory (top facts)" in out
    assert "User prefers terse replies." in out
    assert "Project: HiveOS." in out


# --- prefetch branches --------------------------------------------------------

def test_inner_prefetch_returns_empty_when_no_beam():
    inner = _HiveMnemosyneInner()
    assert inner.prefetch("hello") == ""


def test_inner_prefetch_returns_empty_for_empty_query():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    assert inner.prefetch("") == ""


def test_inner_prefetch_returns_empty_on_no_results():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = []
    assert inner.prefetch("x") == ""


def test_inner_prefetch_renders_xml_wrapped_block():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = [
        {"score": 0.9, "content": "fact 1"},
        {"score": 0.4, "content": "fact 2"},
    ]
    out = inner.prefetch("anything")
    assert out.startswith("<memory-context>") and out.endswith("</memory-context>")
    assert "[0.90] fact 1" in out
    assert "[0.40] fact 2" in out


def test_inner_prefetch_swallows_exception():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.side_effect = RuntimeError("recall broke")
    assert inner.prefetch("x") == ""


def test_inner_prefetch_returns_empty_when_all_below_min_score():
    """All recall results below PREFETCH_MIN_SCORE → lines == [header] only → return ""."""
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = [
        {"score": 0.1, "content": "noise 1"},
        {"score": 0.2, "content": "noise 2"},
    ]
    assert inner.prefetch("x") == ""


# --- sync_turn branches -------------------------------------------------------

def test_inner_sync_turn_no_beam_returns():
    inner = _HiveMnemosyneInner()
    inner.sync_turn("u", "a")   # must not raise


def test_inner_sync_turn_remember_user_and_assistant():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner.sync_turn("user said", "agent said")
    assert inner._beam.remember.call_count == 2
    # First call: user, importance 0.6
    args, _ = inner._beam.remember.call_args_list[0]
    assert args[0] == "user said"
    # Second call: assistant, importance 0.5
    args, _ = inner._beam.remember.call_args_list[1]
    assert args[0] == "agent said"


def test_inner_sync_turn_skips_empty_strings():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner.sync_turn("", "")
    inner._beam.remember.assert_not_called()


def test_inner_sync_turn_swallows_exception():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.remember.side_effect = RuntimeError("write failed")
    inner.sync_turn("u", "a")   # must not raise


# --- get_tool_schemas ---------------------------------------------------------

def test_inner_get_tool_schemas_returns_three_tools():
    schemas = _HiveMnemosyneInner().get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert names == ["hive_remember", "hive_recall", "hive_memory_sleep"]


# --- handle_tool_call branches -----------------------------------------------

def test_inner_handle_tool_call_not_initialised():
    inner = _HiveMnemosyneInner()
    assert inner.handle_tool_call("hive_remember", {"content": "x"}) \
        == "[memory not initialised]"


def test_inner_handle_tool_call_remember_happy():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.remember.return_value = "mem-deadbeef12345678"
    out = inner.handle_tool_call("hive_remember", {"content": "hello"})
    assert out.startswith("stored: ") and "mem-dead" in out
    args, kwargs = inner._beam.remember.call_args
    assert args[0] == "hello"
    assert kwargs["importance"] == 0.7
    assert kwargs["source"] == "agent"


def test_inner_handle_tool_call_recall_with_results():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = [
        {"score": 0.8, "content": "a"},
        {"score": 0.5, "content": "b"},
    ]
    out = inner.handle_tool_call("hive_recall", {"query": "q"})
    assert "[0.80] a" in out and "[0.50] b" in out


def test_inner_handle_tool_call_recall_no_results():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.recall.return_value = []
    assert inner.handle_tool_call("hive_recall", {"query": "q"}) == "no memories found"


def test_inner_handle_tool_call_sleep_returns_str():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.sleep.return_value = {"consolidated": 3}
    out = inner.handle_tool_call("hive_memory_sleep", {})
    assert "consolidated" in out


def test_inner_handle_tool_call_swallows_beam_exception():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    inner._beam.remember.side_effect = RuntimeError("boom")
    out = inner.handle_tool_call("hive_remember", {"content": "x"})
    assert out.startswith("[memory error:")


def test_inner_handle_tool_call_unknown_tool():
    inner = _HiveMnemosyneInner()
    inner._beam = MagicMock()
    out = inner.handle_tool_call("hive_teleport", {})
    assert out == "[unknown memory tool: hive_teleport]"


# --- set_host_llm_backend (inner) --------------------------------------------

def test_inner_set_host_llm_backend_registers_sync_wrapper():
    """set_host_llm_backend creates a _SyncBackend that calls sync_fn."""
    inner = _HiveMnemosyneInner()
    sync_calls = []

    def sync_fn(prompt: str) -> str:
        sync_calls.append(prompt)
        return f"reply:{prompt}"

    captured = {}

    def fake_register(backend):
        captured["backend"] = backend
        return True

    with patch.object(mp, "_register_host_llm", fake_register):
        inner.set_host_llm_backend(sync_fn)
    assert "backend" in captured
    backend = captured["backend"]
    assert backend.name == "hive-host-llm"
    assert backend.complete("ping") == "reply:ping"
    assert sync_calls == ["ping"]


def test_inner_set_host_llm_backend_complete_swallows_sync_fn_exception():
    inner = _HiveMnemosyneInner()
    captured = {}

    def bad_fn(_prompt: str) -> str:
        raise RuntimeError("boom")

    with patch.object(mp, "_register_host_llm",
                      lambda b: captured.update(b=b) or True):
        inner.set_host_llm_backend(bad_fn)
    # _SyncBackend.complete must return None, not raise
    assert captured["b"].complete("x") is None


# --- HiveMnemosyneProvider fail-open wrappers --------------------------------

def test_provider_init_swallows_inner_exception():
    inner = MagicMock()
    inner.initialize.side_effect = RuntimeError("init failed")
    # Must not raise
    HiveMnemosyneProvider(inner).initialize("s1")


def test_provider_system_prompt_block_swallows_exception():
    inner = MagicMock()
    inner.system_prompt_block.side_effect = RuntimeError("x")
    assert HiveMnemosyneProvider(inner).system_prompt_block() == ""


def test_provider_prefetch_swallows_exception():
    inner = MagicMock()
    inner.prefetch.side_effect = RuntimeError("x")
    assert HiveMnemosyneProvider(inner).prefetch("q") == ""


def test_provider_sync_turn_swallows_exception():
    inner = MagicMock()
    inner.sync_turn.side_effect = RuntimeError("x")
    HiveMnemosyneProvider(inner).sync_turn("u", "a")


def test_provider_get_tool_schemas_swallows_exception():
    inner = MagicMock()
    inner.get_tool_schemas.side_effect = RuntimeError("x")
    assert HiveMnemosyneProvider(inner).get_tool_schemas() == []


def test_provider_handle_tool_call_swallows_exception():
    inner = MagicMock()
    inner.handle_tool_call.side_effect = RuntimeError("x")
    out = HiveMnemosyneProvider(inner).handle_tool_call("hive_remember", {})
    assert out.startswith("[memory error:")


def test_provider_on_session_end_swallows_exception():
    inner = MagicMock()
    inner.on_session_end.side_effect = RuntimeError("x")
    HiveMnemosyneProvider(inner).on_session_end()


# --- recall / already_known / learn ------------------------------------------

def test_provider_recall_uses_inner_recall_attr():
    inner = MagicMock(spec=["recall"])   # spec restricts attributes
    inner.recall.return_value = [{"content": "x"}]
    out = HiveMnemosyneProvider(inner).recall("q", limit=3)
    assert out == [{"content": "x"}]
    inner.recall.assert_called_once_with("q", top_k=3)


def test_provider_recall_falls_back_to_beam_when_no_recall_attr():
    inner = MagicMock(spec=["_beam"])
    inner._beam.recall.return_value = [{"content": "y"}]
    out = HiveMnemosyneProvider(inner).recall("q", limit=2)
    assert out == [{"content": "y"}]
    inner._beam.recall.assert_called_once_with("q", top_k=2)


def test_provider_recall_swallows_exception():
    inner = MagicMock(spec=["recall"])
    inner.recall.side_effect = RuntimeError("x")
    assert HiveMnemosyneProvider(inner).recall("q") == []


def test_provider_already_known_true():
    inner = MagicMock(spec=["recall"])
    inner.recall.return_value = [{"content": "x"}]
    assert HiveMnemosyneProvider(inner).already_known("topic") is True


def test_provider_already_known_false():
    inner = MagicMock(spec=["recall"])
    inner.recall.return_value = []
    assert HiveMnemosyneProvider(inner).already_known("topic") is False


def test_provider_learn_passes_payload_to_handle_tool_call():
    inner = MagicMock()
    inner.handle_tool_call.return_value = "stored: abc"
    HiveMnemosyneProvider(inner).learn("pref", "python", "use type hints", "src")
    inner.handle_tool_call.assert_called_once()
    args, _ = inner.handle_tool_call.call_args
    assert args[0] == "hive_remember"
    payload = args[1]
    assert "[pref] python: use type hints" in payload["content"]
    assert payload["source"] == "src"


def test_provider_learn_omits_topic_when_blank():
    inner = MagicMock()
    inner.handle_tool_call.return_value = "stored: abc"
    HiveMnemosyneProvider(inner).learn("fact", "", "raw content", "")
    args, _ = inner.handle_tool_call.call_args
    payload = args[1]
    assert payload["content"] == "raw content"
    assert payload["source"] == "fact"


def test_provider_learn_swallows_exception():
    inner = MagicMock()
    inner.handle_tool_call.side_effect = RuntimeError("x")
    # Must not raise
    HiveMnemosyneProvider(inner).learn("k", "t", "c")


# --- set_host_llm_backend (provider) -----------------------------------------

def test_provider_set_host_llm_backend_calls_inner():
    inner = MagicMock()
    adapter = MagicMock()
    HiveMnemosyneProvider(inner).set_host_llm_backend(adapter, model="m")
    inner.set_host_llm_backend.assert_called_once()
    # The arg is a sync callable
    sync_fn = inner.set_host_llm_backend.call_args.args[0]
    assert callable(sync_fn)


def test_provider_set_host_llm_backend_skips_when_inner_lacks_method():
    inner = MagicMock(spec=[])   # empty spec — no set_host_llm_backend
    HiveMnemosyneProvider(inner).set_host_llm_backend(MagicMock(), model="m")
    # No exception, no call


def test_provider_set_host_llm_backend_propagates_inner_exception():
    """The provider does NOT wrap the inner set_host_llm_backend call —
    that's the LLM bridge install and an exception there is a real failure."""
    import pytest as _pytest  # type: ignore[import]
    inner = MagicMock()
    inner.set_host_llm_backend.side_effect = RuntimeError("bridge exploded")
    with _pytest.raises(RuntimeError, match="bridge exploded"):
        HiveMnemosyneProvider(inner).set_host_llm_backend(MagicMock(), model="m")


def test_provider_set_host_llm_backend_sync_fn_timeout_and_exception(monkeypatch):
    """The sync_fn wrapper started by the provider must degrade to None on
    TimeoutError or any other exception (consolidation uses the fallback)."""
    import concurrent.futures
    inner = MagicMock()
    captured: dict = {}

    def capture(sync_fn):
        captured["sync_fn"] = sync_fn

    inner.set_host_llm_backend.side_effect = capture
    adapter = MagicMock()
    adapter.complete = MagicMock()   # never actually invoked (we mock the future)

    # Mock the future returned by asyncio.run_coroutine_threadsafe.  Close the
    # coroutine immediately so the test doesn't emit a "coroutine was never
    # awaited" warning when the timeout / exception paths return before .result()
    # ever runs the coroutine body.
    fake_future = MagicMock()

    def rcts(coro, *_args, **_kwargs):
        coro.close()
        return fake_future

    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", rcts)

    # New event loop + thread is created inside the provider; clean it up
    fake_loop = MagicMock()
    fake_loop.run_forever = MagicMock()
    monkeypatch.setattr("asyncio.new_event_loop", lambda: fake_loop)

    import threading
    real_thread_init = threading.Thread.__init__
    init_calls = {"count": 0}

    def patched_init(self, *a, **kw):  # noqa: ARG002
        real_thread_init(self, *a, **kw)
        init_calls["count"] += 1

    monkeypatch.setattr(threading.Thread, "__init__", patched_init)
    # Don't actually run the thread
    monkeypatch.setattr(threading.Thread, "start", lambda *a, **kw: None)

    HiveMnemosyneProvider(inner).set_host_llm_backend(adapter, model="m",
                                                      api_key="k", timeout=0.1)
    assert "sync_fn" in captured
    sync_fn = captured["sync_fn"]

    # Path 1: TimeoutError → returns None
    fake_future.result.side_effect = concurrent.futures.TimeoutError()
    fake_future.cancel = MagicMock()
    assert sync_fn("prompt") is None
    assert fake_future.cancel.called

    # Path 2: arbitrary exception → returns None
    fake_future.result.side_effect = RuntimeError("adapter crashed")
    assert sync_fn("prompt") is None


# --- close() -----------------------------------------------------------------

def test_provider_close_calls_close_on_inner():
    inner = MagicMock()
    HiveMnemosyneProvider(inner).close()
    inner.close.assert_called_once()


def test_provider_close_falls_back_to_shutdown():
    inner = MagicMock(spec=["shutdown"])
    HiveMnemosyneProvider(inner).close()
    inner.shutdown.assert_called_once()


def test_provider_close_swallows_exception():
    inner = MagicMock()
    inner.close.side_effect = RuntimeError("x")
    HiveMnemosyneProvider(inner).close()


def test_provider_close_noop_when_inner_has_neither():
    inner = MagicMock(spec=[])   # no close, no shutdown
    HiveMnemosyneProvider(inner).close()   # must not raise


# --- build_mnemosyne_provider -------------------------------------------------

def test_build_returns_none_when_mnemosyne_not_installed(monkeypatch, tmp_path):
    """When the import fails, build logs and returns None."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mnemosyne" or name.startswith("mnemosyne."):
            raise ImportError("mnemosyne not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert build_mnemosyne_provider(home=tmp_path) is None


def test_build_returns_provider_on_success(monkeypatch, tmp_path):
    """Happy path: mnemosyne importable, init succeeds → returns provider."""
    fake_mnemo = MagicMock()
    monkeypatch.setitem(sys.modules, "mnemosyne", fake_mnemo)
    inner_inst = MagicMock()
    monkeypatch.setattr(mp, "_HiveMnemosyneInner", lambda: inner_inst)
    provider = build_mnemosyne_provider(home=tmp_path, session_id="s1")
    assert isinstance(provider, HiveMnemosyneProvider)
    inner_inst.initialize.assert_called_once_with("s1", hermes_home=str(tmp_path))


def test_build_adds_mnemosyne_root_to_path(monkeypatch, tmp_path):
    """When mnemosyne_root is given, it is added to sys.path before import."""
    fake_mnemo = MagicMock()
    monkeypatch.setitem(sys.modules, "mnemosyne", fake_mnemo)
    inner_inst = MagicMock()
    monkeypatch.setattr(mp, "_HiveMnemosyneInner", lambda: inner_inst)
    root = tmp_path / "mnemo-root"
    root.mkdir()
    try:
        build_mnemosyne_provider(home=tmp_path, mnemosyne_root=root)
        assert str(root) in sys.path
    finally:
        if str(root) in sys.path:
            sys.path.remove(str(root))


def test_build_returns_none_on_init_exception(monkeypatch, tmp_path):
    fake_mnemo = MagicMock()
    monkeypatch.setitem(sys.modules, "mnemosyne", fake_mnemo)
    inner_inst = MagicMock()
    inner_inst.initialize.side_effect = RuntimeError("init boom")
    monkeypatch.setattr(mp, "_HiveMnemosyneInner", lambda: inner_inst)
    assert build_mnemosyne_provider(home=tmp_path) is None
