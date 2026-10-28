"""Publish a verified release selection to a host-owned systemd credential source.

This command is installed with the host launcher, outside release worktrees.
It never selects a candidate or restarts a service. A mismatch between this
file and the SQLite catalog makes the gateway launcher refuse to start.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from hive.core.deployment_identity import deployment_host_key
from hive.core.deployment_ledger import DeployLedger
from hive.core.installed_release import InstalledReleaseSelector

_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _open_protected_directory(path: Path, trusted_uid: int) -> int:
    """Walk from / without following links or trusting a replaceable ancestor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in (*path.parts[1:], None):
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, trusted_uid}
                    or (info.st_mode & 0o022 and not (
                        info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                    ))):
                raise ValueError("credential path contains an untrusted directory")
            if component is not None:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ValueError("credential path contains an untrusted directory") from exc
    except BaseException:
        os.close(descriptor)
        raise


def publish_selected_release(
    selector: InstalledReleaseSelector, credential_source: Path, *,
    expected_sha: str, trusted_uid: int = 0,
) -> str:
    """Replace only a trusted regular credential file with the verified SHA.

    ``trusted_uid`` allows a scratch directory owned by the test runner; the
    host command always uses UID 0 and requires an effective root process.
    """
    if os.name != "posix":
        raise ValueError("host selection publication requires POSIX")
    if not isinstance(expected_sha, str) or _FULL_SHA.fullmatch(expected_sha) is None:
        raise ValueError("operator must provide one full lowercase release SHA")
    source = Path(credential_source)
    if (not source.is_absolute() or source.name != "selected-release"
            or ".." in source.parent.parts):
        raise ValueError("credential source must be in a canonical absolute directory")
    selected_sha, _ = selector.selected_release()
    if selected_sha != expected_sha:
        raise ValueError("operator SHA differs from selected release")
    directory_fd = _open_protected_directory(source.parent, trusted_uid)
    temporary: str | None = None
    try:
        parent = os.fstat(directory_fd)
        if parent.st_uid != trusted_uid or parent.st_mode & 0o022:
            raise ValueError("credential directory is not host-owned and protected")
        try:
            current = os.stat(source.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (current is not None and (not stat.S_ISREG(current.st_mode)
                                     or current.st_uid != trusted_uid
                                     or current.st_mode & 0o022)):
            raise ValueError("credential source is not a protected regular file")
        temporary = f".selected-release.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                     os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
        try:
            data = f"{selected_sha}\n".encode("ascii")
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("credential write did not progress")
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        # A concurrent catalog change before publication is refused. A later
        # change is caught by the launcher's independent credential comparison.
        if selector.selected_release()[0] != expected_sha:
            raise ValueError("selected release changed during publication")
        os.replace(temporary, source.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)
    return selected_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a host-owned HiveOS release selection")
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--credential-source", required=True)
    parser.add_argument("--expected-sha", required=True)
    options = parser.parse_args(argv)
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise ValueError("host publication requires root")
        root = Path(options.release_root)
        state_db = Path(options.state_db)
        if (not root.is_absolute() or root.resolve(strict=True) != root
                or not state_db.is_absolute() or state_db.resolve().is_relative_to(root)
                or state_db.is_symlink()):
            raise ValueError("release catalog or state database path is invalid")
        selector = InstalledReleaseSelector(
            root, DeployLedger(state_db), host_key=deployment_host_key(root), scope="system",
        )
        publish_selected_release(selector, Path(options.credential_source),
                                 expected_sha=options.expected_sha)
    except (OSError, ValueError, sqlite3.DatabaseError):
        parser.exit(1, "host selection publication refused\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
