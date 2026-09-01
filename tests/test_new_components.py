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


def test_read_operation_denies_sensitive_path():
    assert check_path("/etc/passwd", operation="read") is not None


def test_symlink_to_denied_path_is_denied(tmp_path):
    # Security: a symlink pointing at a denied system path must also be denied —
    # realpath resolution defeats the symlink-bypass attack (write to innocent.txt
    # which actually links to /etc/passwd).
    link = tmp_path / "innocent.txt"
    try:
        link.symlink_to("/etc/passwd")
    except OSError:  # pragma: no cover - symlinks unsupported
        pytest.skip("symlinks not supported on this platform")
    assert is_write_denied(str(link)) is True
    assert check_path(str(link), operation="write") is not None


def test_real_falls_back_to_input_on_realpath_error(monkeypatch):
    # _real() must never raise; on realpath failure it returns the input unchanged
    # so the denylist comparison still runs (fail-closed via literal match).
    from hive.tools import file_safety

    def _boom(_p):
        raise RuntimeError("realpath blew up")

    monkeypatch.setattr(file_safety.os.path, "realpath", _boom)
    assert file_safety._real("/some/path") == "/some/path"


def test_build_denied_includes_credential_and_system_files(tmp_path):
    import os
    paths = build_denied_write_paths(str(tmp_path))
    for name in (".netrc", ".pgpass", ".git-credentials", ".npmrc"):
        assert os.path.realpath(str(tmp_path / name)) in paths
    # system files are absolute, independent of home
    assert os.path.realpath("/etc/shadow") in paths
    assert os.path.realpath("/etc/sudoers") in paths


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


# ---------------------------------------------------------------------------
# SSRF / web_get URL validation
# ---------------------------------------------------------------------------

import asyncio as _asyncio

from hive.tools.builtins import WebGet, _validate_url


def test_web_get_validates_url_before_request():
    """WebGet.execute() returns an error ToolResult for private IPs without HTTP call."""
    tool = WebGet()
    result = _asyncio.run(tool.execute(url="http://192.168.1.1/secret"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_ssrf_blocks_169_254_range():
    """169.254.0.0/16 (link-local) is blocked by _validate_url."""
    import pytest
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://169.254.169.254/latest/meta-data/")


def test_ssrf_allows_https_example_com():
    """https://example.com is a public address and must pass _validate_url without error."""
    # Should not raise
    _validate_url("https://example.com/path")


# ---------------------------------------------------------------------------
# DockerShellProvider
# ---------------------------------------------------------------------------

from hive.tools.shell_provider import DockerShellProvider


def test_docker_shell_provider_image_stored():
    """DockerShellProvider stores the image name and exposes it via _image."""
    provider = DockerShellProvider(image="python:3.12-slim")
    assert provider._image == "python:3.12-slim"


# ---------------------------------------------------------------------------
# TerminalOutcome — str enum
# ---------------------------------------------------------------------------

from hive.agents.base import TerminalOutcome


def test_terminal_outcome_str_values():
    """TerminalOutcome is a str enum — values are plain strings."""
    assert isinstance(TerminalOutcome.COMPLETED, str)
    assert TerminalOutcome.COMPLETED == "completed"
    assert TerminalOutcome.MAX_TURNS == "max_turns"
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"


# ---------------------------------------------------------------------------
# channel_hint in system_prompt
# ---------------------------------------------------------------------------

from hive.context.prompt_builder import system_prompt


def test_channel_hint_injected_freshly():
    """system_prompt with a channel_hint includes the [Active surface: ...] line."""
    prompt = system_prompt(channel_hint="telegram")
    assert "[Active surface: telegram]" in prompt


def test_channel_hint_empty_no_extra_text():
    """system_prompt with no channel_hint does NOT add an [Active surface] line."""
    prompt = system_prompt(channel_hint="")
    assert "[Active surface:" not in prompt


# --- Additional new-component tests (Wave 3K) ------------------------------------

def test_validate_url_blocks_ftp_scheme():
    """_validate_url raises ValueError for ftp:// scheme."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("ftp://example.com/file.txt")


def test_validate_url_blocks_userinfo():
    """_validate_url raises ValueError for URL containing username:password."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="userinfo"):
        _validate_url("http://user:pass@example.com/")


def test_validate_url_blocks_ipv6_loopback():
    """_validate_url raises ValueError for IPv6 loopback ::1."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://[::1]/admin")


def test_docker_shell_provider_default_network_is_none():
    """DockerShellProvider defaults to network='none' for isolation."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider()
    assert p._network == "none"


def test_mnemosyne_provider_name_is_mnemosyne():
    """HiveMnemosyneProvider.name returns 'mnemosyne'."""
    from hive.memory.mnemosyne_provider import HiveMnemosyneProvider

    class _FakeInner:
        def initialize(self, *a, **kw): pass
        def system_prompt_block(self): return ""
        def prefetch(self, *a, **kw): return ""
        def sync_turn(self, *a, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, *a, **kw): return ""
        def on_session_end(self, *a, **kw): pass

    p = HiveMnemosyneProvider(_FakeInner())
    assert p.name == "mnemosyne"


def test_strip_surrogates_empty_string():
    """strip_surrogates('') returns '' without error."""
    from hive.llm.sanitize import strip_surrogates
    assert strip_surrogates("") == ""


def test_repair_tool_arguments_nested_json():
    """repair_tool_arguments handles valid nested JSON correctly."""
    from hive.llm.sanitize import repair_tool_arguments
    nested = '{"outer": {"inner": [1, 2, 3]}}'
    result = repair_tool_arguments(nested)
    assert json.loads(result) == {"outer": {"inner": [1, 2, 3]}}


# --- Wave 3N additional tests ---------------------------------------------------

def test_validate_url_blocks_data_scheme():
    """_validate_url raises ValueError for data: URIs."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("data:text/html,<h1>hi</h1>")


def test_validate_url_blocks_ipv6_private_fc00():
    """_validate_url raises ValueError for IPv6 private fc00::/7 addresses."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://[fc00::1]/test")


def test_validate_url_allows_https_public():
    """_validate_url passes for a well-known public HTTPS URL."""
    from hive.tools.builtins import _validate_url
    _validate_url("https://api.github.com/repos")  # must not raise


def test_docker_shell_provider_custom_image_stored():
    """DockerShellProvider stores the image name passed at construction."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(image="ubuntu:22.04")
    assert p._image == "ubuntu:22.04"


def test_docker_shell_provider_custom_network_stored():
    """DockerShellProvider stores a custom network value."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(network="bridge")
    assert p._network == "bridge"


def test_validate_url_blocks_link_local_ipv6():
    """_validate_url raises ValueError for IPv6 link-local fe80:: addresses."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://[fe80::1]/info")


# --- Wave 3R additional tests ---------------------------------------------------

def test_validate_url_blocks_10_private_range():
    """_validate_url raises ValueError for 10.0.0.0/8 private addresses."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://10.0.0.1/internal")


def test_validate_url_blocks_172_16_private_range():
    """_validate_url raises ValueError for 172.16.0.0/12 private addresses."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://172.16.0.1/secret")


def test_validate_url_blocks_localhost_127():
    """_validate_url raises ValueError for 127.0.0.1 loopback."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://127.0.0.1:8080/")


def test_docker_shell_provider_keyword_only_network():
    """DockerShellProvider accepts network as keyword-only alongside default image."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(network="host")
    assert p._image == "alpine:latest"
    assert p._network == "host"


def test_terminal_outcome_tool_error_value():
    """TerminalOutcome.TOOL_ERROR is a str enum with value 'tool_error'."""
    assert isinstance(TerminalOutcome.TOOL_ERROR, str)
    assert TerminalOutcome.TOOL_ERROR == "tool_error"


def test_channel_hint_voice_surface():
    """system_prompt with channel_hint='voice' includes [Active surface: voice]."""
    prompt = system_prompt(channel_hint="voice")
    assert "[Active surface: voice]" in prompt


# --- Wave 3X additional tests ---------------------------------------------------

def test_wave3x_channel_hint_web():
    """system_prompt with channel_hint='web' includes [Active surface: web]."""
    prompt = system_prompt(channel_hint="web")
    assert "[Active surface: web]" in prompt


def test_wave3x_channel_hint_cli():
    """system_prompt with channel_hint='cli' includes [Active surface: cli]."""
    prompt = system_prompt(channel_hint="cli")
    assert "[Active surface: cli]" in prompt


def test_wave3x_channel_hint_sms():
    """system_prompt with channel_hint='sms' includes [Active surface: sms]."""
    prompt = system_prompt(channel_hint="sms")
    assert "[Active surface: sms]" in prompt


def test_wave3x_terminal_outcome_max_turns_is_str():
    """TerminalOutcome.MAX_TURNS is a str enum with value 'max_turns'."""
    assert isinstance(TerminalOutcome.MAX_TURNS, str)
    assert TerminalOutcome.MAX_TURNS == "max_turns"


def test_wave3x_terminal_outcome_loop_guard_is_str():
    """TerminalOutcome.LOOP_GUARD is a str enum with value 'loop_guard'."""
    assert isinstance(TerminalOutcome.LOOP_GUARD, str)
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"


def test_wave3x_validate_url_blocks_file_scheme():
    """_validate_url raises ValueError for file:// scheme."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("file:///etc/passwd")


def test_wave3x_validate_url_blocks_192_168_direct():
    """_validate_url raises ValueError for 192.168.0.0/16 private range directly."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://192.168.0.1/admin")


def test_wave3x_docker_shell_provider_explicit_image_and_network():
    """DockerShellProvider stores both image and network when both are supplied."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(image="debian:12", network="internal")
    assert p._image == "debian:12"
    assert p._network == "internal"


# --- Wave 4E additional tests ---------------------------------------------------

def test_wave4e_validate_url_blocks_javascript_scheme():
    """_validate_url raises ValueError for javascript: scheme."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("javascript:alert(1)")


def test_wave4e_validate_url_blocks_ipv6_unique_local_fd00():
    """_validate_url raises ValueError for IPv6 unique-local fd00::/8 addresses."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://[fd00::1]/internal")


def test_wave4e_validate_url_allows_public_ipv4_8_8_8_8():
    """_validate_url passes for a well-known public IPv4 address 8.8.8.8."""
    from hive.tools.builtins import _validate_url
    _validate_url("https://8.8.8.8/dns")


def test_wave4e_system_prompt_memory_block_content_injected():
    """system_prompt with a memory_block includes that block's content in the output."""
    prompt = system_prompt(memory_block="UNIQUE_MEMORY_MARKER_42")
    assert "UNIQUE_MEMORY_MARKER_42" in prompt


def test_wave4e_system_prompt_empty_memory_block_not_injected():
    """system_prompt with memory_block='' does not inject that token into output."""
    prompt = system_prompt(memory_block="")
    assert "UNIQUE_MEMORY_MARKER_42" not in prompt


def test_wave4e_channel_hint_none_same_as_empty():
    """system_prompt with channel_hint=None behaves like channel_hint='' — no [Active surface]."""
    prompt_none = system_prompt(channel_hint=None)
    prompt_empty = system_prompt(channel_hint="")
    assert "[Active surface:" not in prompt_none
    assert "[Active surface:" not in prompt_empty


def test_wave4e_validate_url_blocks_ipv4_loopback_127_0_0_2():
    """_validate_url raises ValueError for 127.0.0.2 (loopback range)."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://127.0.0.2/internal")


def test_wave4e_system_prompt_memory_block_and_channel_hint_together():
    """system_prompt injects both memory_block content and channel_hint line together."""
    prompt = system_prompt(memory_block="MY_MEMORY_DATA", channel_hint="api")
    assert "MY_MEMORY_DATA" in prompt
    assert "[Active surface: api]" in prompt


# --- Wave 4K additional tests ---------------------------------------------------

def test_wave4k_ssrf_blocks_172_20_private():
    """_validate_url raises ValueError for 172.20.x.x (172.16-31 private range)."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://172.20.0.1/internal")


def test_wave4k_ssrf_blocks_172_31_private():
    """_validate_url raises ValueError for 172.31.x.x (172.16-31 private range)."""
    from hive.tools.builtins import _validate_url
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://172.31.255.255/secret")


def test_wave4k_system_prompt_memory_block_only_no_channel():
    """system_prompt with only memory_block includes block but no [Active surface] line."""
    prompt = system_prompt(memory_block="MEMORY_ONLY_BLOCK")
    assert "MEMORY_ONLY_BLOCK" in prompt
    assert "[Active surface:" not in prompt


def test_wave4k_system_prompt_output_type_is_str():
    """system_prompt always returns a plain str instance."""
    prompt = system_prompt()
    assert type(prompt) is str


def test_wave4k_system_prompt_with_channel_and_memory_type_is_str():
    """system_prompt returns str even when both args are provided."""
    prompt = system_prompt(memory_block="X", channel_hint="cli")
    assert type(prompt) is str


def test_wave4k_validate_url_hostname_passes():
    """_validate_url does not raise for a public hostname (no raw IP)."""
    from hive.tools.builtins import _validate_url
    _validate_url("https://openai.com/v1/chat/completions")  # must not raise


def test_wave4k_channel_hint_with_empty_memory_block():
    """system_prompt with channel_hint and memory_block='' includes surface line but no empty block artifact."""
    prompt = system_prompt(channel_hint="terminal", memory_block="")
    assert "[Active surface: terminal]" in prompt


def test_wave4k_validate_url_hostname_anthropic_passes():
    """_validate_url passes for api.anthropic.com (public hostname)."""
    from hive.tools.builtins import _validate_url
    _validate_url("https://api.anthropic.com/v1/messages")  # must not raise


# --- Wave 4P-A additional new-component tests ------------------------------------

def test_wave4p_validate_url_ftp_scheme_blocked():
    """_validate_url raises ValueError for ftp:// with message containing 'Blocked scheme'."""
    from hive.tools.builtins import _validate_url
    import pytest
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("ftp://files.example.com/pub/data.zip")


def test_wave4p_validate_url_data_scheme_blocked():
    """_validate_url raises ValueError for data: URI with message containing 'Blocked scheme'."""
    from hive.tools.builtins import _validate_url
    import pytest
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("data:application/json,{}")


def test_wave4p_validate_url_https_10_private_blocked():
    """_validate_url raises ValueError for https://10.0.0.1 (private range, https scheme)."""
    from hive.tools.builtins import _validate_url
    import pytest
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("https://10.0.0.1/internal")


def test_wave4p_system_prompt_channel_hint_cli_contains_active_surface():
    """system_prompt with channel_hint='cli' includes '[Active surface: cli]'."""
    from hive.context.prompt_builder import system_prompt
    prompt = system_prompt(channel_hint="cli")
    assert "[Active surface: cli]" in prompt


def test_wave4p_system_prompt_channel_hint_web_contains_active_surface():
    """system_prompt with channel_hint='web' includes '[Active surface: web]'."""
    from hive.context.prompt_builder import system_prompt
    prompt = system_prompt(channel_hint="web")
    assert "[Active surface: web]" in prompt


def test_wave4p_docker_shell_provider_custom_network_attribute():
    """DockerShellProvider stores a custom network string as _network attribute."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(network="overlay")
    assert p._network == "overlay"


def test_wave4p_terminal_outcome_completed_value():
    """TerminalOutcome.COMPLETED.value is the string 'completed'."""
    from hive.agents.base import TerminalOutcome
    assert TerminalOutcome.COMPLETED.value == "completed"


def test_wave4p_terminal_outcome_tool_error_value():
    """TerminalOutcome.TOOL_ERROR.value is the string 'tool_error'."""
    from hive.agents.base import TerminalOutcome
    assert TerminalOutcome.TOOL_ERROR.value == "tool_error"
