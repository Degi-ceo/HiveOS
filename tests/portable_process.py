"""Portable real subprocess commands for tests of CLI and shell behavior."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def python_exec_command(code: str) -> str:
    """Return a command suitable for run_codex's shlex/exec argv parser."""
    return shlex.join((Path(sys.executable).as_posix(), "-c", code))


def python_shell_command(code: str) -> str:
    """Return a command understood by the host's default shell."""
    if os.name == "nt":
        return _windows_python_shell_command(sys.executable, code)
    return shlex.join((sys.executable, "-c", code))


def _windows_python_shell_command(executable: str, code: str) -> str:
    """Quote for cmd.exe even when an argument has metacharacters but no spaces."""
    def quoted(value: str) -> str:
        rendered = subprocess.list2cmdline((value,))
        return rendered if rendered.startswith('"') else f'"{rendered}"'

    return f"{quoted(executable)} -c {quoted(code)}"
