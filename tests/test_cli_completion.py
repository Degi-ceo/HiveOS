"""test_cli_completion.py — SPRINT_6 P-J J3 shell completion coverage.

Targets 100% coverage on:
  src/hive/surfaces/cli/completion.py
Plus dispatch tests for the _completion handler and _build_help_overview
in src/hive/surfaces/cli/__init__.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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
    assert "complete -F _hive_complete hive" in out


def test_bash_completion_includes_all_command_names(sample):
    out = bash_completion(sample)
    for spec in sample:
        assert spec.name in out


def test_bash_completion_handles_no_subcommands(sample):
    out = bash_completion([CompletionSpec(name="solo", category="core", help="x")])
    assert "solo" in out


def test_bash_completion_emits_subcommand_completer(sample):
    """The single Hive completer dispatches after the command word."""
    out = bash_completion(sample)
    assert "learning)" in out
    assert 'COMP_CWORD == index + 1' in out


# --- zsh ---

def test_zsh_completion_starts_with_compdef(sample):
    out = zsh_completion(sample)
    assert out.startswith("#compdef hive")


def test_zsh_completion_lists_subcommands(sample):
    out = zsh_completion(sample)
    assert "subcommands=( 'status' 'replay' )" in out


def test_zsh_completion_emits_per_subcommand_function(sample):
    """The loaded function dispatches subcommands at the current word."""
    out = zsh_completion(sample)
    assert "learning)" in out
    assert "CURRENT == index + 1" in out


# --- fish ---

def test_fish_completion_returns_string(sample):
    out = fish_completion(sample)
    assert isinstance(out, str)
    assert "complete -c hive" in out


def test_fish_completion_includes_subcommands(sample):
    out = fish_completion(sample)
    assert "__hive_completion_context) = learning" in out
    assert "-a 'status replay'" in out


def test_completion_rejects_shell_metacharacters_in_command_names():
    bad = [CompletionSpec(name="x$(danger)", category="core", help="x")]
    for generator in (bash_completion, zsh_completion, fish_completion):
        with pytest.raises(ValueError, match="invalid completion command name"):
            generator(bad)


def test_zsh_display_description_cannot_execute_shell_text():
    spec = CompletionSpec(name="safe", category="core", help="`danger` ' $() :")
    out = zsh_completion([spec])
    assert "`danger`" not in out
    assert "$(" not in out
    assert "'safe:" in out


def _available_shell(name: str) -> str:
    candidates = [shutil.which(name)]
    if name == "bash":
        candidates.insert(0, os.environ.get("HIVE_TEST_BASH"))
        if os.name == "nt" and os.environ.get("ProgramFiles"):
            candidates.append(str(Path(os.environ["ProgramFiles"]) / "Git" / "bin" / "bash.exe"))
    for executable in candidates:
        if executable is None or not Path(executable).is_file():
            continue
        probe = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=5,
            check=False,
        )
        if probe.returncode == 0:
            return executable
    pytest.skip(f"{name} is unavailable or cannot start")


def test_bash_real_completion_dispatches_after_global_option(sample):
    bash = _available_shell("bash")
    script = bash_completion(sample)
    cases = (
        ('COMP_WORDS=(hive ""); COMP_CWORD=1', {"--no-color", "chat", "ask", "learning"}),
        ('COMP_WORDS=(hive --no-color lea); COMP_CWORD=2', {"learning"}),
        ('COMP_WORDS=(hive learning ""); COMP_CWORD=2', {"status", "replay"}),
        ('COMP_WORDS=(hive --no-color learning sta); COMP_CWORD=3', {"status"}),
    )
    for setup, expected in cases:
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-s"],
            input=script + "\n" + setup + '\n_hive_complete\nprintf "%s\\n" "${COMPREPLY[@]}"\n',
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert set(result.stdout.splitlines()) == expected


def test_zsh_script_parses_in_real_shell(sample):
    zsh = _available_shell("zsh")
    result = subprocess.run(
        [zsh, "-n"], input=zsh_completion(sample),
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_zsh_real_dispatch_selects_command_and_subcommands(sample):
    zsh = _available_shell("zsh")
    script = zsh_completion(sample)
    capture_candidates = (
        'function _describe() {\n'
        '    if [[ "$2" == commands ]]; then\n'
        '        print -l -- "${commands[@]}"\n'
        '    else\n'
        '        print -l -- "${subcommands[@]}"\n'
        '    fi\n'
        '}\n'
    )
    for words, current, expected in (
        ('hive ""', 2, {"--no-color", "chat", "ask", "learning"}),
        ('hive --no-color lea', 3, {"--no-color", "chat", "ask", "learning"}),
        ('hive learning sta', 3, {"status", "replay"}),
        ('hive --no-color learning sta', 4, {"status", "replay"}),
    ):
        result = subprocess.run(
            [zsh, "-f", "-s"],
            input=capture_candidates + f"words=({words})\nCURRENT={current}\n" + script,
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        candidates = {line.split(":", 1)[0] for line in result.stdout.splitlines()}
        assert candidates == expected


def test_fish_real_completion_selects_subcommands(sample):
    fish = _available_shell("fish")
    script = fish_completion(sample)
    for command, expected in (
        ("hive ", {"--no-color", "chat", "ask", "learning"}),
        ("hive --no-color learning ", {"status", "replay"}),
    ):
        result = subprocess.run(
            [fish, "--no-config", "-c", f"source /dev/stdin; complete -C '{command}'"],
            input=script, capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        candidates = {line.split("\t", 1)[0] for line in result.stdout.splitlines()}
        assert expected <= candidates


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
