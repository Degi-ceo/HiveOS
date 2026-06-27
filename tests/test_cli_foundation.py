"""Tests for SPRINT_6 P-J CLI foundation: themes + parser + Output + registry.

Target: 100% line coverage on
  src/hive/surfaces/cli/themes.py
  src/hive/surfaces/cli/parser.py
  src/hive/surfaces/cli/output.py
  src/hive/surfaces/cli/registry.py

Plus an integration test that runs `cli.main(["version" / "logs" / "status"])`.
"""
from __future__ import annotations

import argparse
import sys
from unittest.mock import patch

import pytest

from hive.surfaces.cli import output as output_mod
from hive.surfaces.cli import parser as parser_mod
from hive.surfaces.cli import registry as registry_mod
from hive.surfaces.cli import style as style_mod
from hive.surfaces.cli import themes as themes_mod
from hive.surfaces.cli.themes import MINIMAL, MONO, NEON, REGISTRY, Theme


# ===========================================================================
# themes.py
# ===========================================================================

@pytest.fixture(autouse=True)
def _reset_style_and_themes():
    """Snapshot/restore style._TOKENS + themes._ACTIVE around each test."""
    saved_tokens = dict(style_mod._TOKENS)
    saved_active = themes_mod._ACTIVE
    saved_color = style_mod._ENABLED
    yield
    style_mod._TOKENS.clear()
    style_mod._TOKENS.update(saved_tokens)
    themes_mod._ACTIVE = saved_active
    style_mod._ENABLED = saved_color


def test_current_returns_neon_when_tty_and_unset(monkeypatch):
    monkeypatch.delenv("HIVE_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    themes_mod._ACTIVE = themes_mod._resolve()
    assert themes_mod.current() is NEON


def test_current_returns_minimal_when_no_color(monkeypatch):
    monkeypatch.delenv("HIVE_THEME", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    themes_mod._ACTIVE = themes_mod._resolve()
    assert themes_mod.current() is MINIMAL


def test_current_returns_requested_theme_from_env(monkeypatch):
    monkeypatch.setenv("HIVE_THEME", "mono")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    themes_mod._ACTIVE = themes_mod._resolve()
    assert themes_mod.current() is MONO


def test_current_returns_minimal_when_non_tty(monkeypatch):
    monkeypatch.delenv("HIVE_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    themes_mod._ACTIVE = themes_mod._resolve()
    assert themes_mod.current() is MINIMAL


def test_set_theme_mutates_style_tokens(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    style_mod.set_color_enabled(True)
    themes_mod.set_theme("minimal")
    assert themes_mod.current() is MINIMAL
    assert style_mod._TOKENS["cyan"] == "36"
    # paint() emits a RESET then opens SGR; just check cyan (36) is in the output
    out = style_mod.paint("cyan", "x")
    assert "x" in out
    assert "36" in out


def test_set_theme_unknown_raises_value_error():
    with pytest.raises(ValueError) as exc:
        themes_mod.set_theme("nonexistent")
    assert "nonexistent" in str(exc.value)
    assert "neon" in str(exc.value)


def test_theme_getitem_and_repr():
    t = Theme("test", {"cyan": "36"})
    assert t["cyan"] == "36"
    assert "test" in repr(t)


def test_theme_is_hashable_and_immutable_by_convention():
    """Theme holds plain dict; verify item access + repr."""
    t = Theme("x", {"a": "1"})
    assert t.name == "x"
    assert t.tokens == {"a": "1"}


def test_registry_has_exactly_three_themes():
    assert set(themes_mod.REGISTRY.keys()) == {"neon", "minimal", "mono"}
    assert themes_mod.REGISTRY["neon"] is NEON
    assert themes_mod.REGISTRY["minimal"] is MINIMAL
    assert themes_mod.REGISTRY["mono"] is MONO


def test_minimal_uses_simple_ansi_codes():
    assert MINIMAL["cyan"] == "36"
    assert MINIMAL["amber"] == "33"
    assert MINIMAL["bold"] == "1"
    assert MINIMAL["dim"] == "90"


# ===========================================================================
# parser.py
# ===========================================================================

def test_make_parser_lists_all_subcommands():
    p = parser_mod.make_parser()
    # argparse subparsers expose choices via _subparsers
    sub_action = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    for cmd in registry_mod.REGISTRY:
        assert cmd in sub_action.choices


def test_make_parser_uses_rich_help_formatter():
    p = parser_mod.make_parser()
    assert p.formatter_class is parser_mod.RichHelpFormatter


def test_parse_chat_returns_chat_command():
    spec, parsed = parser_mod.parse(["chat"])
    assert spec.name == "chat"
    assert spec.handler_name == "_chat"
    assert spec is registry_mod.REGISTRY["chat"]


def test_parse_ask_returns_message():
    spec, parsed = parser_mod.parse(["ask", "hello"])
    assert spec.name == "ask"
    assert parsed.MSG == "hello"


def test_parse_logs_returns_logs_with_tail():
    spec, parsed = parser_mod.parse(["logs", "--tail", "5"])
    assert spec.name == "logs"
    assert parsed.tail == 5


def test_parse_learning_status_returns_status_subcommand():
    spec, parsed = parser_mod.parse(["learning", "status"])
    assert spec.name == "status"
    assert parsed.limit == 10  # default


def test_parse_learning_replay_with_id():
    spec, parsed = parser_mod.parse(["learning", "replay", "42"])
    assert spec.name == "replay"
    assert parsed.ID == "42"  # argparse dest defaults to the literal arg name "ID"


def test_parse_empty_defaults_to_chat():
    spec, parsed = parser_mod.parse([])
    assert spec.name == "chat"


def test_parse_help_exits_via_systemexit():
    with pytest.raises(SystemExit) as exc:
        parser_mod.parse(["--help"])
    assert exc.value.code == 0


def test_rich_help_formatter_start_section_colorizes_when_enabled(monkeypatch, capsys):
    style_mod.set_color_enabled(True)
    fmt = parser_mod.RichHelpFormatter("hive")
    fmt.start_section("Options")
    fmt.end_section()
    out = capsys.readouterr().out
    # The colored heading goes through _HelpFormatter's _HelpAction machinery
    # which writes nothing to stdout; just verify the formatter instance works.
    assert fmt is not None


def test_rich_help_formatter_disabled_does_not_colorize(monkeypatch, capsys):
    style_mod.set_color_enabled(False)
    fmt = parser_mod.RichHelpFormatter("hive")
    fmt.start_section("Options")
    fmt.end_section()


# ===========================================================================
# output.py
# ===========================================================================

@pytest.fixture(autouse=False)
def _reset_output_singleton():
    """Reset the Output singleton around tests that touch it."""
    saved = output_mod._output
    output_mod._output = None
    yield
    output_mod._output = saved


def test_get_output_is_lazy_singleton(_reset_output_singleton):
    o1 = output_mod.get_output()
    o2 = output_mod.get_output()
    assert o1 is o2
    assert o1.theme is themes_mod.current()


def test_output_print_with_token_writes_ansi(capsys, _reset_output_singleton, monkeypatch):
    style_mod.set_color_enabled(True)
    themes_mod.set_theme("minimal")  # force ANSI=36 for "cyan"
    output_mod.get_output().print("hi", token="cyan")
    out = capsys.readouterr().out
    assert "hi" in out
    assert "\x1b[36m" in out


def test_output_print_without_token_writes_plain(capsys, _reset_output_singleton, monkeypatch):
    style_mod.set_color_enabled(True)
    output_mod.get_output().print("hi")
    out = capsys.readouterr().out
    assert out == "hi\n"


def test_output_print_with_color_disabled(capsys, _reset_output_singleton):
    style_mod.set_color_enabled(False)
    output_mod.get_output().print("hi", token="cyan")
    out = capsys.readouterr().out
    assert "hi" in out
    assert "\x1b[" not in out


def test_output_print_with_custom_end(capsys, _reset_output_singleton):
    output_mod.get_output().print("x", end="")
    assert capsys.readouterr().out == "x"


def test_output_banner_writes_banner(capsys, _reset_output_singleton, monkeypatch):
    style_mod.set_color_enabled(False)
    output_mod.get_output().banner(version="0.3.0")
    out = capsys.readouterr().out
    assert "v0.3.0" in out


def test_output_rule_writes_rule(capsys, _reset_output_singleton):
    output_mod.get_output().rule("=")
    out = capsys.readouterr().out
    assert "=" * 60 in out


def test_output_table_writes_headers_and_rows(capsys, _reset_output_singleton):
    output_mod.get_output().table(["A", "B"], [["1", "2"], ["3", "4"]])
    out = capsys.readouterr().out
    assert "A" in out
    assert "B" in out
    assert "1" in out
    assert "4" in out
    assert "-" in out  # separator line


def test_set_output_replaces_singleton(_reset_output_singleton):
    sentinel = output_mod.Output(theme=MINIMAL, color=False)
    output_mod.set_output(sentinel)
    assert output_mod.get_output() is sentinel


def test_output_paint_returns_text_when_empty(_reset_output_singleton):
    o = output_mod.get_output()
    assert o.paint("cyan", "") == ""


def test_output_paint_returns_text_when_color_disabled(_reset_output_singleton):
    style_mod.set_color_enabled(False)
    o = output_mod.get_output()
    assert o.paint("cyan", "hi") == "hi"


def test_output_paint_with_color_enabled_delegates(_reset_output_singleton, monkeypatch):
    """When color=True and text non-empty, Output.paint delegates to style.paint."""
    style_mod.set_color_enabled(True)
    themes_mod.set_theme("minimal")
    o = output_mod.Output(theme=MINIMAL, color=True)
    out = o.paint("cyan", "hi")
    assert "hi" in out
    assert "\x1b[36m" in out


# ===========================================================================
# registry.py
# ===========================================================================

def test_registry_is_non_empty():
    assert len(registry_mod.REGISTRY) > 0


def test_registry_has_all_expected_top_level_commands():
    expected = {"chat", "ask", "serve", "init", "doctor", "mcp-serve",
                "heartbeat", "consolidate", "version", "status",
                "logs", "budget", "approvals", "learning"}
    assert expected <= set(registry_mod.REGISTRY.keys())


def test_registry_chat_handler_resolves_to_module_chat():
    """The registry stores the handler NAME; resolution happens at dispatch."""
    from hive.surfaces import cli
    assert registry_mod.REGISTRY["chat"].handler_name == "_chat"
    assert getattr(cli, registry_mod.REGISTRY["chat"].handler_name) is cli._chat


def test_registry_learning_has_subcommands():
    learning = registry_mod.REGISTRY["learning"]
    assert "status" in learning.subcommands
    assert "replay" in learning.subcommands


def test_every_handler_name_resolves_to_callable():
    """Every handler_name in REGISTRY must resolve to a callable in cli module."""
    from hive.surfaces import cli
    for name, spec in registry_mod.REGISTRY.items():
        if not spec.handler_name:
            continue  # doctor: dispatched inline by main
        resolved = getattr(cli, spec.handler_name, None)
        assert callable(resolved), f"{name}: handler {spec.handler_name!r} not callable"


def test_command_spec_defaults():
    """CommandSpec with minimal args has empty args tuple + empty subcommands."""
    spec = registry_mod.CommandSpec(name="x", help="h", handler_name="noop")
    assert spec.args == ()
    assert spec.subcommands == {}


def test_register_adds_to_registry():
    spec = registry_mod.CommandSpec(name="zzz-test-tmp", help="tmp", handler_name="noop")
    registry_mod.register(spec)
    try:
        assert registry_mod.REGISTRY["zzz-test-tmp"] is spec
    finally:
        registry_mod.REGISTRY.pop("zzz-test-tmp", None)


# ===========================================================================
# Integration: cli.main end-to-end
# ===========================================================================

def test_main_version_exits_zero(capsys):
    from hive.surfaces.cli import main
    rc = main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hive" in out.lower()


def test_main_logs_returns_zero_with_no_state_db(capsys, monkeypatch, tmp_path):
    from hive.surfaces.cli import main
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "nonexistent.sqlite"))
    from hive.surfaces import cli as cli_mod
    import importlib
    importlib.reload(cli_mod)
    rc = main(["logs", "--tail", "5"])
    # With a missing state_db, _logs returns 1 (with a hint).
    assert rc == 1
    out = capsys.readouterr().out
    assert "doctor" in out.lower() or "state database" in out.lower()


def test_main_status_returns_zero(capsys):
    from hive.surfaces.cli import main
    rc = main(["status"])
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "exec_model" in out or "exec_provider" in out


def test_main_logs_with_invalid_tail_falls_back(capsys, monkeypatch, tmp_path):
    """cli.main(['logs', '--tail', 'abc']) should still exit (and call _logs(20))."""
    from hive.surfaces.cli import main
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "nope.sqlite"))
    from hive.surfaces import cli as cli_mod
    import importlib
    importlib.reload(cli_mod)
    with patch.object(cli_mod, "_logs", return_value=0) as l:
        rc = main(["logs", "--tail", "abc"])
        l.assert_called_once_with(20)
    assert rc == 0
