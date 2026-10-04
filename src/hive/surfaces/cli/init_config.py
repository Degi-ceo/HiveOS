"""Fail-closed, side-effect-bounded first-run dotenv setup."""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import warnings
from pathlib import Path

from dotenv import dotenv_values

from hive.core.config import env_file_path
from hive.core.soul import REPO_ROOT

_TARGET_KEYS = ("MINIMAX_API_KEY", "HIVE_SECRET", "MNEMOSYNE_HOME")
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
_MISSING_API_KEYS = frozenset({"", "YOUR_KEY_HERE", "your-key-here"})
_MISSING_SECRETS = frozenset({"", "change_me", "change-me", "your-secret-here"})
_MAX_ENV_BYTES = 262_144
_MAX_VALUE_CHARS = 4096


class _InitError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unsafe_link(path: Path) -> bool:
    if os.name == "nt":
        try:
            attributes = path.lstat().st_file_attributes
        except FileNotFoundError:
            return False
        except AttributeError as exc:
            raise _InitError("unsafe_env_path") from exc
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return path.is_symlink()


def _has_linked_ancestor(path: Path) -> bool:
    return any(_unsafe_link(part) for part in (path, *path.parents))


def _read_existing(path: Path) -> tuple[str, bool, bool]:
    if _has_linked_ancestor(path):
        raise _InitError("unsafe_env_path")
    if not path.parent.is_dir():
        raise _InitError("config_directory_missing")
    if not path.exists():
        return "", False, True
    if not path.is_file():
        raise _InitError("unsafe_env_path")
    info = path.stat()
    if info.st_size > _MAX_ENV_BYTES:
        raise _InitError("env_file_too_large")
    with path.open("r", encoding="utf-8", newline="") as stream:
        content = stream.read()
    secure = os.name == "nt" or stat.S_IMODE(info.st_mode) & 0o077 == 0
    return content, True, secure


def _values(content: str) -> dict[str, str]:
    counts: dict[str, int] = {}
    for line in content.splitlines():
        match = _KEY_LINE.match(line)
        if match and match.group(1) in _TARGET_KEYS:
            key = match.group(1)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1:
                raise _InitError("duplicate_config_key")
    parsed = dotenv_values(stream=io.StringIO(content), interpolate=False)
    return {key: value for key, value in parsed.items() if isinstance(value, str)}


def _safe_value(value: str) -> str:
    if (
        len(value) > _MAX_VALUE_CHARS
        or any(char in value for char in "\r\n\x00")
        or "${" in value
    ):
        raise _InitError("invalid_config_value")
    return value


def _set_values(content: str, updates: dict[str, str]) -> str:
    lines = content.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in content else "\n"
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = _KEY_LINE.match(line)
        key = match.group(1) if match else ""
        if key in updates:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            result.append(f"{line[:match.end()]}{json.dumps(updates[key], ensure_ascii=False)}{ending}")
            seen.add(key)
        else:
            result.append(line)
    for key in _TARGET_KEYS:
        if key in updates and key not in seen:
            if result and not result[-1].endswith(("\r", "\n")):
                result.append(newline)
            result.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}{newline}")
    return "".join(result)


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".hive-env-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _result(*, ok: bool, changed: bool = False, path: Path | None = None,
            error: str = "", json_output: bool = False) -> int:
    if json_output:
        payload: dict[str, object] = {"ok": ok}
        if ok:
            payload.update(changed=changed, env_file=str(path))
        else:
            payload["error"] = error
        print(json.dumps(payload, ensure_ascii=False))
    elif ok:
        print(f"HiveOS configuration {'saved' if changed else 'already configured'}: {path}")
        print("Diagnostics and memory seeding were not run. Use `hive doctor` explicitly.")
    else:
        print(f"HiveOS setup stopped ({error}); no configuration was saved.", file=sys.stderr)
    return 0 if ok else 130 if error == "cancelled" else 1


def run_init(*, non_interactive: bool = False, json_output: bool = False) -> int:
    """Collect all input before a single atomic dotenv replacement.

    No doctor, memory seeding, network call, or secret-bearing output occurs.
    """
    try:
        path = env_file_path(REPO_ROOT)
        content, exists, permissions_secure = _read_existing(path)
        existing = _values(content)
        updates: dict[str, str] = {}

        key = existing.get("MINIMAX_API_KEY", "")
        if key in _MISSING_API_KEYS:
            key = os.getenv("MINIMAX_API_KEY", "")
        if key in _MISSING_API_KEYS:
            if non_interactive:
                raise _InitError("missing_api_key")
            if not sys.stdin.isatty():
                raise _InitError("tty_required")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("  MINIMAX_API_KEY (hidden): ").strip()
        if key in _MISSING_API_KEYS:
            raise _InitError("missing_api_key")
        key = _safe_value(key)
        if existing.get("MINIMAX_API_KEY", "") != key:
            updates["MINIMAX_API_KEY"] = key

        secret = existing.get("HIVE_SECRET", "")
        if secret in _MISSING_SECRETS:
            secret = os.getenv("HIVE_SECRET", "")
        if secret in _MISSING_SECRETS:
            secret = secrets.token_hex(24)
        secret = _safe_value(secret)
        if existing.get("HIVE_SECRET", "") != secret:
            updates["HIVE_SECRET"] = secret

        memory_home = existing.get("MNEMOSYNE_HOME", "") or os.getenv("MNEMOSYNE_HOME", "")
        if not memory_home:
            memory_home = existing.get("HIVE_MNEMOSYNE_HOME", "")
        if not memory_home and not non_interactive:
            if not sys.stdin.isatty():
                raise _InitError("tty_required")
            memory_home = input("  MNEMOSYNE_HOME (optional; Enter for runtime default)> ").strip()
        if memory_home:
            memory_home = _safe_value(memory_home)
            if existing.get("MNEMOSYNE_HOME", "") != memory_home:
                updates["MNEMOSYNE_HOME"] = memory_home

        updated = _set_values(content, updates)
        changed = not exists or updated != content or not permissions_secure
        if changed:
            _atomic_write(path, updated)
        return _result(ok=True, changed=changed, path=path, json_output=json_output)
    except (KeyboardInterrupt, EOFError):
        return _result(ok=False, error="cancelled", json_output=json_output)
    except getpass.GetPassWarning:
        return _result(ok=False, error="tty_required", json_output=json_output)
    except _InitError as exc:
        return _result(ok=False, error=exc.code, json_output=json_output)
    except (OSError, UnicodeError, ValueError):
        return _result(ok=False, error="io_error", json_output=json_output)
