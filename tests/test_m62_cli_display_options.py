"""Global display option coverage for the operator CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hive.surfaces import cli
from hive.surfaces.cli import parser, style
from hive.surfaces.cli.completion import bash_completion, fish_completion, zsh_completion


def test_parser_exposes_no_color_before_command():
    spec, args = parser.parse(["--no-color", "version"])
    assert spec.name == "version"
    assert args.no_color is True


def test_shell_completion_exposes_no_color():
    for generator in (bash_completion, zsh_completion, fish_completion):
        assert "--no-color" in generator([]) or "-l no-color" in generator([])


def test_no_color_disables_categorized_help_and_restores_state(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    original = style.is_color_enabled()
    style.set_color_enabled(True)
    try:
        assert cli.main(["--no-color", "--help"]) == 0
        output = capsys.readouterr().out
        assert "HiveOS terminal surface" in output
        assert "\x1b[" not in output
        assert "NO_COLOR" not in os.environ
        assert style.is_color_enabled()
    finally:
        style.set_color_enabled(original)


def test_no_color_reaches_legacy_chat_renderer(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    async def fake_chat(session_id=None):
        print(cli._cyan("legacy colored text"))
        return 0

    monkeypatch.setattr(cli, "_chat", fake_chat)
    assert cli.main(["--no-color", "chat"]) == 0
    assert capsys.readouterr().out == "legacy colored text\n"
    assert "NO_COLOR" not in os.environ


def test_no_color_restores_existing_env_and_style_on_failure(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "previous")
    original = style.is_color_enabled()
    style.set_color_enabled(True)

    def fail():
        raise RuntimeError("sentinel")

    monkeypatch.setattr(cli, "_build_help_overview", fail)
    try:
        with pytest.raises(RuntimeError, match="sentinel"):
            cli.main(["--no-color", "--help"])
        assert os.environ["NO_COLOR"] == "previous"
        assert style.is_color_enabled()
    finally:
        style.set_color_enabled(original)


def test_no_color_help_in_subprocess(tmp_path):
    """The module entry point accepts the option from this checkout in a real process."""
    import subprocess

    env = dict(os.environ, HIVE_DATA_DIR=str(tmp_path))
    env.pop("NO_COLOR", None)
    env.pop("HIVE_NO_COLOR", None)
    source = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(source), env.get("PYTHONPATH", ""))))
    result = subprocess.run(
        [sys.executable, "-m", "hive.surfaces.cli", "--no-color", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "HiveOS terminal surface" in result.stdout
    assert "--no-color" in result.stdout
    assert "\x1b[" not in result.stdout
