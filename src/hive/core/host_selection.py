"""Read the selected release SHA from a systemd-provided credential.

The credential's authority comes from a host-owned systemd unit and
``LoadCredential=`` source. This parser only validates its local bytes and path;
it cannot establish filesystem ownership from an arbitrary environment.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

_CREDENTIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n?\Z")


def read_host_selected_sha(
    name: str, *, environment: Mapping[str, str] | None = None,
) -> str:
    """Read exactly one full lowercase commit ID without following a file link."""
    if not isinstance(name, str) or not _CREDENTIAL_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("invalid host selection credential name")
    env = os.environ if environment is None else environment
    raw_dir = env.get("CREDENTIALS_DIRECTORY", "")
    directory = Path(raw_dir)
    if (not raw_dir or not directory.is_absolute()
            or directory.resolve(strict=True) != directory):
        raise ValueError("systemd credential directory is unavailable")
    path = directory / name
    path_info = path.lstat()
    if stat.S_ISLNK(path_info.st_mode) or (
        getattr(path_info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError("host selection credential cannot be a link")
    if not stat.S_ISREG(path_info.st_mode):
        raise ValueError("host selection credential must be a regular file")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    if os.name == "posix":
        flags |= os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65:
            raise ValueError("host selection credential must be a bounded regular file")
        value = os.read(fd, 66)
    finally:
        os.close(fd)
    if len(value) != info.st_size or _FULL_SHA.fullmatch(value) is None:
        raise ValueError("host selection credential must contain one full lowercase SHA")
    return value.rstrip(b"\n").decode("ascii")
