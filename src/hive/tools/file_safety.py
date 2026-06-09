"""
file_safety.py — sensitive-path denylist for tool execution.

Ported from Hermes agent/file_safety.py (HERMES_REFERENCE REUSE-READY #13,
Direct/Zero). The ToolExecutor checks candidate file paths against this list
before dispatching write/execute tool calls so the agent cannot overwrite
credentials, shell config, or the PROTECTED HiveOS files.
"""
from __future__ import annotations

import os
from pathlib import Path


def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except Exception:  # noqa: BLE001
        return p


def build_denied_write_paths(home: str | None = None) -> frozenset[str]:
    """Return absolute real paths that must never be written by a tool call."""
    h = home or os.path.expanduser("~")
    paths = {
        # SSH
        os.path.join(h, ".ssh", "authorized_keys"),
        os.path.join(h, ".ssh", "id_rsa"),
        os.path.join(h, ".ssh", "id_ed25519"),
        os.path.join(h, ".ssh", "config"),
        # Shell init
        os.path.join(h, ".bashrc"),
        os.path.join(h, ".zshrc"),
        os.path.join(h, ".profile"),
        os.path.join(h, ".bash_profile"),
        os.path.join(h, ".zprofile"),
        # Credential files
        os.path.join(h, ".netrc"),
        os.path.join(h, ".pgpass"),
        os.path.join(h, ".npmrc"),
        os.path.join(h, ".pypirc"),
        os.path.join(h, ".git-credentials"),
        # System
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
        "/etc/hosts",
        # HiveOS PROTECTED files (extra guard in addition to self_mod checks)
        _real("Config/SOUL.md"),
        _real("Core/approval_gate.py"),
    }
    return frozenset(_real(p) for p in paths)


# Module-level singleton built once; tools executor imports this.
DENIED_WRITE_PATHS: frozenset[str] = build_denied_write_paths()


def is_write_denied(path: str) -> bool:
    """True if writing to `path` is forbidden."""
    return _real(path) in DENIED_WRITE_PATHS


def check_path(path: str, *, operation: str = "write") -> str | None:
    """Return an error string if `path` is off-limits, else None."""
    if operation in ("write", "delete", "move") and is_write_denied(path):
        return f"writing to {path!r} is not permitted (sensitive path)"
    return None
