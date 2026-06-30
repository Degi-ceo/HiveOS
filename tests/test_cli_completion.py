"""test_cli_completion.py — SPRINT_6 P-J J3 shell completion coverage.

Targets 100% coverage on:
  src/hive/surfaces/cli/completion.py
Plus dispatch tests for the _completion handler and _build_help_overview
in src/hive/surfaces/cli/__init__.py.
"""
from __future__ import annotations

import pytest

from hive.surfaces.cli.completion import (
    CompletionSpec,
    bash_completion,
    fish_completion,
    zsh_completion,
)


@pytest.fixture
def sample():
    return [
        CompletionSpec(name="chat", category="core", help="Start REPL"),
        CompletionSpec(name="ask", category="core", help="One-shot question"),
        CompletionSpec(
            name="learning", category="runtime", help="Inspect learning loop",
            subcommands=("status", "replay"),
        ),
    ]


# --- bash ---

def test_bash_completion_returns_string(sample):
    out = bash_completion(sample)
    assert isinstance(out, str)
    assert out.startswith("# bash completion for hive")
    assert "_hive_commands()" in out or "complete -F _hive_commands hive" in out


def test_bash_completion_includes_all_command_names(sample):
    out = bash_completion(sample)
    for spec in sample:
        assert spec.name in out


def test_bash_completion_handles_no_subcommands(sample):
    out = bash_completion([CompletionSpec(name="solo", category="core", help="x")])
    assert "solo" in out


def test_bash_completion_emits_subcommand_completer(sample):
    """Commands with subcommands get a per-command complete -F entry."""
    out = bash_completion(sample)
    assert "complete -F _hive_learning" in out
    assert "_hive_learning" in out


# --- zsh ---

def test_zsh_completion_starts_with_compdef(sample):
    out = zsh_completion(sample)
    assert out.startswith("#compdef hive")


def test_zsh_completion_lists_subcommands(sample):
    out = zsh_completion(sample)
    assert "learning:status" in out or "learning:replay" in out


def test_zsh_completion_emits_per_subcommand_function(sample):
    """A command with subcommands gets a dedicated _hive_<name> function."""
    out = zsh_completion(sample)
    assert "_hive_learning" in out


# --- fish ---

def test_fish_completion_returns_string(sample):
    out = fish_completion(sample)
    assert isinstance(out, str)
    assert "complete -c hive" in out


def test_fish_completion_includes_subcommands(sample):
    out = fish_completion(sample)
    assert "learning status" in out
    assert "learning replay" in out


# --- edge cases ---

def test_empty_command_list_returns_minimal_valid_script():
    for fn in (bash_completion, zsh_completion, fish_completion):
        out = fn([])
        assert isinstance(out, str)
        # minimal self-contained script (no Python eval)
        assert "python" not in out
        assert "hive" in out


def test_completion_spec_is_frozen():
    s = CompletionSpec(name="x", category="y", help="z")
    with pytest.raises(Exception):
        s.name = "changed"  # type: ignore[misc]


# ===========================================================================
# Dispatch + help overview tests for src/hive/surfaces/cli/__init__.py
# ===========================================================================

def test_completion_command_dispatches_bash(capsys):
    from hive.surfaces.cli import _completion
    rc = _completion(["bash"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "complete -F" in out


def test_completion_command_dispatches_zsh(capsys):
    from hive.surfaces.cli import _completion
    assert _completion(["zsh"]) == 0
    assert "#compdef hive" in capsys.readouterr().out


def test_completion_command_dispatches_fish(capsys):
    from hive.surfaces.cli import _completion
    assert _completion(["fish"]) == 0
    assert "complete -c hive" in capsys.readouterr().out


def test_completion_unknown_shell_returns_2(capsys):
    from hive.surfaces.cli import _completion
    assert _completion(["tcsh"]) == 2
    assert "unknown shell" in capsys.readouterr().err.lower()


def test_help_overview_groups_by_category(capsys):
    from hive.surfaces.cli import _build_help_overview
    _build_help_overview()
    out = capsys.readouterr().out
    # Category headers from the J3 taxonomy
    found_any = False
    for cat in ("core", "runtime", "ops", "library", "gateway"):
        if cat in out:
            found_any = True
            break
    assert found_any, "no category headers found in help overview"
