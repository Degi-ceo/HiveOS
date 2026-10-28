"""A systemd selection credential must contain one exact full commit ID."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hive.core.host_selection import read_host_selected_sha

_SHA = b"a" * 40


@pytest.mark.parametrize("sha", [b"a" * 40, b"b" * 64])
def test_reads_exact_selected_sha_with_optional_newline(tmp_path: Path, sha: bytes):
    credential = tmp_path / "selected_release"
    for value in (sha, sha + b"\n"):
        credential.write_bytes(value)
        assert read_host_selected_sha(
            "selected_release", environment={"CREDENTIALS_DIRECTORY": str(tmp_path)},
        ) == sha.decode("ascii")


@pytest.mark.parametrize("value", [
    b"", b"short", b"A" * 40, b"a" * 39, b"a" * 41, b"a" * 66,
    _SHA + b"\r\n", _SHA + b"\nother", _SHA + b"\0",
])
def test_rejects_malformed_or_extra_credential_bytes(tmp_path: Path, value: bytes):
    (tmp_path / "selected_release").write_bytes(value)
    with pytest.raises(ValueError, match="credential"):
        read_host_selected_sha(
            "selected_release", environment={"CREDENTIALS_DIRECTORY": str(tmp_path)},
        )


def test_rejects_missing_directory_or_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="directory"):
        read_host_selected_sha("selected_release", environment={})
    with pytest.raises(ValueError, match="name"):
        read_host_selected_sha("../selected_release",
                               environment={"CREDENTIALS_DIRECTORY": str(tmp_path)})


def test_rejects_linked_credential(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(_SHA)
    link = tmp_path / "selected_release"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable to this test account")
    with pytest.raises(ValueError, match="link"):
        read_host_selected_sha(
            "selected_release", environment={"CREDENTIALS_DIRECTORY": str(tmp_path)},
        )


@pytest.mark.skipif(os.name != "posix", reason="named FIFOs are a POSIX file type")
def test_rejects_fifo_credential_without_blocking(tmp_path: Path):
    os.mkfifo(tmp_path / "selected_release")
    with pytest.raises(ValueError, match="regular file"):
        read_host_selected_sha(
            "selected_release", environment={"CREDENTIALS_DIRECTORY": str(tmp_path)},
        )
