"""Opt-in pytest temp compatibility for restricted Windows process tokens.

Python 3.12.4+ gives ``mkdir(mode=0o700)`` an owner-only Windows DACL.
That DACL omits the restricting SID of a sandbox token, so pytest and
``tempfile`` cannot reopen directories they just created.  Only an explicitly
selected, per-run directory beneath the user's OS temp directory is affected.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_ENV_NAME = "HIVE_PYTEST_SANDBOX_TEMP_ROOT"
_PREFIX = "hive-pytest-sandbox-"
_original_mkdir = os.mkdir


def inherited_mode(path: object, mode: int, root: Path, *, dir_fd: int | None = None) -> int:
    """Keep private-mode creation everywhere except the selected test root."""
    if mode != 0o700 or dir_fd is not None or isinstance(path, bytes):
        return mode
    try:
        candidate = Path(path).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return mode
    return 0o777 if candidate.is_relative_to(root) else mode


def activate() -> None:
    """Use the inherited ACL only for an explicit Windows sandbox test run."""
    raw_root = os.environ.get(_ENV_NAME)
    if not raw_root:
        return
    if os.name != "nt":
        raise RuntimeError(f"{_ENV_NAME} is supported only on Windows")
    root = Path(raw_root)
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError(f"{_ENV_NAME} must name a real absolute directory")
    root = root.resolve(strict=True)
    os_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if root.parent != os_temp or re.fullmatch(rf"{_PREFIX}[0-9a-f]{{32}}", root.name) is None:
        raise RuntimeError(f"{_ENV_NAME} must name a dedicated child of the OS temp directory")

    def sandbox_mkdir(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        selected_mode = inherited_mode(path, mode, root, dir_fd=dir_fd)
        if dir_fd is None:
            _original_mkdir(path, selected_mode)
        else:
            _original_mkdir(path, selected_mode, dir_fd=dir_fd)

    os.mkdir = sandbox_mkdir
    tempfile.tempdir = str(root)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(root)
