"""Host-owned credential publication is atomic and fails closed."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hive.core.deployment_identity import deployment_host_key
from hive.core.deployment_ledger import DeployLedger
from hive.core.host_selection import read_host_selected_sha
from hive.core.host_selection_publish import main, publish_selected_release
from hive.core.installed_release import InstalledReleaseSelector

pytestmark = pytest.mark.skipif(os.name != "posix", reason="host publication uses POSIX ownership")


def _selected(tmp_path: Path) -> tuple[Path, InstalledReleaseSelector, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=source, capture_output=True,
                                text=True, check=True)
        return result.stdout.strip()
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("config", "user.name", "Hive test")
    git("config", "user.email", "hive@example.invalid")
    entrypoint = source / "src" / "hive" / "surfaces" / "cli" / "__main__.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("print('first')\n", encoding="ascii")
    git("add", "src")
    git("commit", "-qm", "first release")
    first = git("rev-parse", "HEAD")
    entrypoint.write_text("print('second')\n", encoding="ascii")
    git("commit", "-qam", "second release")
    second = git("rev-parse", "HEAD")
    root = tmp_path / "installed"
    (root / "releases").mkdir(parents=True)
    for sha in (first, second):
        git("worktree", "add", "--detach", "-q", str(root / "releases" / sha), sha)
    selector = InstalledReleaseSelector(
        root, DeployLedger(tmp_path / "state.sqlite"),
        host_key=deployment_host_key(root), scope="system",
    )
    selector.select_candidate(first, expected_selected=None)
    return root, selector, first, second


def _destination(tmp_path: Path) -> Path:
    directory = tmp_path / "host-etc"
    directory.mkdir(mode=0o700)
    return directory / "selected-release"


def test_publishes_exact_verified_selection_and_replaces_atomically(tmp_path: Path):
    root, selector, first, second = _selected(tmp_path)
    destination = _destination(tmp_path)
    assert publish_selected_release(selector, destination, expected_sha=first,
                                    trusted_uid=os.getuid()) == first
    assert destination.read_bytes() == f"{first}\n".encode()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert read_host_selected_sha(
        "selected-release", environment={"CREDENTIALS_DIRECTORY": str(destination.parent)},
    ) == first
    selector.select_candidate(second, expected_selected=first)
    assert publish_selected_release(selector, destination, expected_sha=second,
                                    trusted_uid=os.getuid()) == second
    assert destination.read_bytes() == f"{second}\n".encode()
    assert sorted(path.name for path in destination.parent.iterdir()) == ["selected-release"]
    assert (root / "releases" / first).is_dir()


def test_rejects_dirty_selected_release_without_touching_credential(tmp_path: Path):
    root, selector, first, _second = _selected(tmp_path)
    destination = _destination(tmp_path)
    destination.write_text("previous\n", encoding="ascii")
    (root / "releases" / first / "src" / "hive" / "surfaces" / "cli" /
     "__main__.py").write_text("changed\n", encoding="ascii")
    with pytest.raises(ValueError, match="clean installed release"):
        publish_selected_release(selector, destination, expected_sha=first,
                                 trusted_uid=os.getuid())
    assert destination.read_text(encoding="ascii") == "previous\n"


def test_operator_sha_must_match_and_be_complete(tmp_path: Path):
    _root, selector, first, second = _selected(tmp_path)
    destination = _destination(tmp_path)
    for supplied in (second, first[:12], first.upper()):
        with pytest.raises(ValueError, match="SHA"):
            publish_selected_release(selector, destination, expected_sha=supplied,
                                     trusted_uid=os.getuid())
        assert not destination.exists()


def test_catalog_change_before_replace_preserves_previous_source(tmp_path: Path):
    _root, selector, first, second = _selected(tmp_path)
    destination = _destination(tmp_path)
    destination.write_text("previous\n", encoding="ascii")
    original = selector.selected_release
    calls = 0

    def changing_release():
        nonlocal calls
        calls += 1
        if calls == 2:
            selector.select_candidate(second, expected_selected=first)
        return original()

    selector.selected_release = changing_release  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="changed during publication"):
        publish_selected_release(selector, destination, expected_sha=first,
                                 trusted_uid=os.getuid())
    assert destination.read_text(encoding="ascii") == "previous\n"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["selected-release"]


def test_rejects_writable_directory_and_linked_source(tmp_path: Path):
    _root, selector, _first, _second = _selected(tmp_path)
    destination = _destination(tmp_path)
    destination.parent.chmod(0o777)
    with pytest.raises(ValueError, match="directory"):
        publish_selected_release(selector, destination, expected_sha=_first,
                                 trusted_uid=os.getuid())
    assert not destination.exists()
    destination.parent.chmod(0o700)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="ascii")
    destination.symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        publish_selected_release(selector, destination, expected_sha=_first,
                                 trusted_uid=os.getuid())
    assert outside.read_text(encoding="ascii") == "keep"


def test_rejects_writable_ancestor_even_if_destination_is_protected(tmp_path: Path):
    _root, selector, first, _second = _selected(tmp_path)
    writable = tmp_path / "gateway-writable"
    writable.mkdir(mode=0o700)
    destination_dir = writable / "host-etc"
    destination_dir.mkdir(mode=0o700)
    writable.chmod(0o777)
    destination = destination_dir / "selected-release"
    with pytest.raises(ValueError, match="untrusted directory"):
        publish_selected_release(selector, destination, expected_sha=first,
                                 trusted_uid=os.getuid())
    assert not destination.exists()


def test_rejects_untrusted_owner_or_noncanonical_path(tmp_path: Path):
    _root, selector, _first, _second = _selected(tmp_path)
    destination = _destination(tmp_path)
    with pytest.raises(ValueError, match="directory"):
        publish_selected_release(selector, destination, expected_sha=_first,
                                 trusted_uid=os.getuid() + 1)
    alias = tmp_path / "alias"
    alias.symlink_to(destination.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="untrusted directory"):
        publish_selected_release(selector, alias / "selected-release",
                                 expected_sha=_first, trusted_uid=os.getuid())
    with pytest.raises(ValueError, match="canonical"):
        publish_selected_release(selector, destination.parent / "other",
                                 expected_sha=_first, trusted_uid=os.getuid())


def test_nonroot_cli_refuses_without_writing(tmp_path: Path, monkeypatch, capsys):
    _root, _selector, _first, _second = _selected(tmp_path)
    destination = _destination(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as exc:
        main(["--release-root", str(tmp_path / "installed"),
              "--state-db", str(tmp_path / "state.sqlite"),
              "--credential-source", str(destination), "--expected-sha", _first])
    assert exc.value.code == 1
    assert capsys.readouterr().err == "host selection publication refused\n"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
                    reason="requires a real root process on Linux")
def test_root_cli_publishes_from_stable_system_catalog(tmp_path: Path):
    root, _selector, first, _second = _selected(tmp_path)
    destination = _destination(tmp_path)
    assert main(["--release-root", str(root),
                 "--state-db", str(tmp_path / "state.sqlite"),
                 "--credential-source", str(destination),
                 "--expected-sha", first]) == 0
    assert destination.read_bytes() == f"{first}\n".encode()
