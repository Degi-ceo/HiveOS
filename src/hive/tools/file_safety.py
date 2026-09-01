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
        return os.path.realpath(os.path.abspath(p))
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
        _real(str(Path(__file__).resolve().parents[3] / "Config" / "SOUL.md")),
        _real(str(Path(__file__).resolve().parents[3] / "Core" / "approval_gate.py")),
    }
    return frozenset(_real(p) for p in paths)


# Module-level singleton built once; tools executor imports this.
DENIED_WRITE_PATHS: frozenset[str] = build_denied_write_paths()

# Secret material is a hard read deny, not an approval flow: approval can prove
# intent to change state but must never expose credentials to a model or caller.
_DENIED_READ_BASENAMES = frozenset({
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials",
    "authorized_keys", "credentials", "credentials.json",
})
_DENIED_READ_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_DENIED_READ_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg", ".kube"})


def _is_env_secret_file(path: str) -> bool:
    """Recognize dotenv secret files while permitting public examples."""
    name = Path(path).name.lower()
    return name == ".env" or (
        name.startswith(".env.") and name not in {".env.example", ".env.template"}
    )


def is_write_denied(path: str) -> bool:
    """True if writing to `path` is forbidden."""
    return _real(path) in DENIED_WRITE_PATHS


def is_read_denied(path: str) -> bool:
    """True if reading ``path`` could disclose credential material."""
    real_path = _real(path)
    path_obj = Path(real_path)
    name = path_obj.name.lower()
    return (
        real_path in DENIED_WRITE_PATHS
        or name in _DENIED_READ_BASENAMES
        or name.endswith(_DENIED_READ_SUFFIXES)
        or _is_env_secret_file(real_path)
        or any(part.lower() in _DENIED_READ_DIRECTORIES for part in path_obj.parts)
    )


def has_traversal(path: str) -> bool:
    """True if the path contains directory traversal sequences (..)."""
    p = Path(path)
    return ".." in p.parts


def has_unsafe_symlink(path: str) -> bool:
    """True if `path` or any parent component is a symlink outside the repo root."""
    try:
        check = Path(path)
        while check != check.parent:
            if check.is_symlink():
                target = check.resolve()
                # Allow symlinks that stay inside the repo root; block escapes.
                try:
                    target.relative_to(Path.cwd())
                except ValueError:
                    return True
            check = check.parent
        return False
    except Exception:  # noqa: BLE001
        return False


def check_path(path: str, *, operation: str = "write") -> str | None:
    """Return an error string if `path` is off-limits, else None."""
    if operation == "read" and is_read_denied(path):
        return f"reading {path!r} is not permitted (sensitive path)"
    if operation in ("write", "delete", "move"):
        if has_traversal(path):
            return f"path traversal not permitted: {path!r}"
        if is_write_denied(path):
            return f"writing to {path!r} is not permitted (sensitive path)"
        if has_unsafe_symlink(path):
            return f"writing through symlink escape is not permitted: {path!r}"
    return None
