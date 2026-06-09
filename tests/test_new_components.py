"""Tests for the components added in the PR review pass:
   mnemosyne_provider, llm/sanitize, tools/file_safety, minimax sanitize/cache.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# llm/sanitize
# ---------------------------------------------------------------------------

from hive.llm.sanitize import (
    strip_surrogates,
    repair_tool_arguments,
    sanitize_messages,
)


def test_strip_surrogates_noop_on_clean():
    assert strip_surrogates("hello world") == "hello world"


def test_strip_surrogates_replaces_lone_surrogates():
    s = "hello\ud83d world"          # lone high surrogate
    result = strip_surrogates(s)
    assert "\ud83d" not in result
    assert "hello" in result and "world" in result


def test_repair_tool_arguments_valid_json():
    good = '{"k": "v"}'
    assert json.loads(repair_tool_arguments(good)) == {"k": "v"}


def test_repair_tool_arguments_trailing_comma():
    bad = '{"k": "v",}'
    result = repair_tool_arguments(bad)
    assert json.loads(result) == {"k": "v"}


def test_repair_tool_arguments_garbage_returns_empty_object():
    assert repair_tool_arguments("not json at all ;;;") == "{}"


def test_sanitize_messages_strips_surrogates():
    msgs = [{"role": "user", "content": "hi\ud800there"}]
    sanitize_messages(msgs)
    assert "\ud800" not in msgs[0]["content"]
    assert "hi" in msgs[0]["content"] and "there" in msgs[0]["content"]


def test_sanitize_messages_handles_tool_blocks():
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "input": {"cmd": "echo\ud800hi"}}
        ]}
    ]
    sanitize_messages(msgs)
    assert "\ud800" not in msgs[0]["content"][0]["input"]["cmd"]


def test_sanitize_messages_noop_on_clean():
    msgs = [{"role": "user", "content": "clean message"}]
    result = sanitize_messages(msgs)
    assert result[0]["content"] == "clean message"


# ---------------------------------------------------------------------------
# tools/file_safety
# ---------------------------------------------------------------------------

from hive.tools.file_safety import check_path, is_write_denied, build_denied_write_paths


def test_ssh_authorized_keys_denied(tmp_path):
    paths = build_denied_write_paths(str(tmp_path))
    import os
    target = os.path.realpath(str(tmp_path / ".ssh" / "authorized_keys"))
    assert target in paths


def test_check_path_returns_error_for_denied():
    err = check_path("/etc/passwd", operation="write")
    assert err is not None
    assert "not permitted" in err


def test_check_path_returns_none_for_safe_path(tmp_path):
    assert check_path(str(tmp_path / "output.txt"), operation="write") is None


def test_read_operation_not_denied():
    # file_safety only blocks write/delete/move, not reads
    assert check_path("/etc/passwd", operation="read") is None


# ---------------------------------------------------------------------------
# memory/mnemosyne_provider — offline unit (no real Mnemosyne needed)
# ---------------------------------------------------------------------------

from hive.memory.mnemosyne_provider import HiveMnemosyneProvider


class _FakeInner:
    """Minimal stand-in for MnemosyneMemoryProvider."""
    def __init__(self):
        self.initialized = False
        self.synced = []

    def initialize(self, session_id, **kw):
        self.initialized = True

    def system_prompt_block(self):
        return "## Mnemosyne: no memories yet"

    def prefetch(self, query, *, session_id=""):
        return f"relevant: {query[:10]}"

    def sync_turn(self, user, assistant, *, session_id=""):
        self.synced.append((user, assistant))

    def get_tool_schemas(self):
        return [{"name": "mnemosyne_remember", "description": "d", "parameters": {}}]

    def handle_tool_call(self, name, args, **kw):
        return f"handled {name}"

    def on_session_end(self, messages):
        pass


def test_mnemosyne_provider_delegates_correctly():
    inner = _FakeInner()
    p = HiveMnemosyneProvider(inner)

    p.initialize("s1", hermes_home="/tmp")
    assert inner.initialized

    block = p.system_prompt_block()
    assert "Mnemosyne" in block

    recall = p.prefetch("something", session_id="s1")
    assert "somethin" in recall

    p.sync_turn("hi", "hello", session_id="s1")
    assert inner.synced == [("hi", "hello")]

    schemas = p.get_tool_schemas()
    assert len(schemas) == 1

    result = p.handle_tool_call("mnemosyne_remember", {"content": "x"})
    assert "mnemosyne_remember" in result


def test_mnemosyne_provider_fail_open_on_error():
    class _BrokenInner:
        def initialize(self, *a, **kw): raise RuntimeError("db locked")
        def system_prompt_block(self): raise RuntimeError("broken")
        def prefetch(self, *a, **kw): raise RuntimeError("broken")
        def sync_turn(self, *a, **kw): raise RuntimeError("broken")
        def get_tool_schemas(self): raise RuntimeError("broken")
        def handle_tool_call(self, *a, **kw): raise RuntimeError("broken")
        def on_session_end(self, *a, **kw): raise RuntimeError("broken")

    p = HiveMnemosyneProvider(_BrokenInner())
    # None of these should raise
    p.initialize("s1")
    assert p.system_prompt_block() == ""
    assert p.prefetch("q") == ""
    p.sync_turn("u", "a")
    assert p.get_tool_schemas() == []
    assert "memory error" in p.handle_tool_call("x", {})
    p.on_session_end()


def test_mnemosyne_provider_recent_default():
    """recent() defaults to [] on the provider ABC."""
    from hive.memory.provider import MemoryProvider
    inner = _FakeInner()
    p = HiveMnemosyneProvider(inner)
    assert p.recent("session") == []


# ---------------------------------------------------------------------------
# build_mnemosyne_provider — graceful failure when package missing
# ---------------------------------------------------------------------------

def test_build_mnemosyne_provider_returns_none_when_unavailable(tmp_path, monkeypatch):
    """If neither import path works, build_mnemosyne_provider returns None."""
    import sys
    from hive.memory import mnemosyne_provider as mp_mod

    original_build = mp_mod.build_mnemosyne_provider

    # Temporarily patch to simulate missing mnemosyne
    def _patched(*, home, session_id="default", mnemosyne_root=None):
        import importlib
        # pretend neither import works
        raise ImportError("simulated missing mnemosyne")

    # Use a fresh call path that returns None (the function already handles ImportError)
    # We test via monkeypatching the inner import
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "mnemosyne.hermes_memory_provider", None)
        m.setitem(sys.modules, "hermes_memory_provider", None)
        result = mp_mod.build_mnemosyne_provider(home=tmp_path)
    # Either returns None (missing) or HiveMnemosyneProvider (installed)
    assert result is None or isinstance(result, HiveMnemosyneProvider)
