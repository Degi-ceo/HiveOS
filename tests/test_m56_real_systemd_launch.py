"""Exercise host selection and the real systemd credential-to-process boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from hive.core.deployment_identity import deployment_host_key
from hive.core.deployment_ledger import DeployLedger
from hive.core.host_selection_publish import publish_selected_release
from hive.core.installed_release import InstalledReleaseSelector

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0
    or os.environ.get("HIVE_TEST_REAL_SYSTEMD") != "1",
    reason="requires an opted-in Linux systemd runner as root",
)


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=source, capture_output=True,
                            text=True, timeout=20, check=True)
    return result.stdout.strip()


def _installed_releases(top: Path) -> tuple[Path, Path, InstalledReleaseSelector, str, str]:
    source = top / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "config", "user.name", "Hive test")
    _git(source, "config", "user.email", "hive@example.invalid")
    entrypoint = source / "src" / "hive" / "surfaces" / "cli" / "__main__.py"
    entrypoint.parent.mkdir(parents=True)
    for parent in (source / "src" / "hive", source / "src" / "hive" / "surfaces",
                   entrypoint.parent):
        (parent / "__init__.py").write_text("", encoding="ascii")
    entrypoint.write_text("import os; print('release-one uid=' + str(os.geteuid()))\n",
                          encoding="ascii")
    _git(source, "add", "src")
    _git(source, "commit", "-qm", "release one")
    first = _git(source, "rev-parse", "HEAD")
    entrypoint.write_text("import os; print('release-two uid=' + str(os.geteuid()))\n",
                          encoding="ascii")
    _git(source, "commit", "-qam", "release two")
    second = _git(source, "rev-parse", "HEAD")
    root = top / "installed"
    (root / "releases").mkdir(parents=True)
    for sha in (first, second):
        _git(source, "worktree", "add", "--detach", "-q", str(root / "releases" / sha), sha)
    data = top / "data"
    data.mkdir(mode=0o700)
    state_db = data / "state.sqlite"
    selector = InstalledReleaseSelector(
        root, DeployLedger(state_db), host_key=deployment_host_key(root), scope="system",
    )
    selector.select_candidate(first, expected_selected=None)
    return root, state_db, selector, first, second


def _install_host_launcher(top: Path) -> Path:
    """Make the launcher's package root-owned and separate from both releases."""
    library = top / "host-lib"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "hive", library / "hive")
    return library


def _run_service(top: Path, root: Path, state_db: Path, credential: Path) -> subprocess.CompletedProcess[str]:
    command = [
        "systemd-run", "--system", "--quiet", "--pipe", "--wait", "--collect",
        f"--unit=hiveos-m56-{uuid.uuid4().hex}",
        "--property=Type=exec", "--property=User=nobody",
        f"--property=WorkingDirectory={root}",
        f"--property=LoadCredential=selected_release:{credential}",
        f"--setenv=HIVE_DATA_DIR={top / 'data'}",
        f"--setenv=HIVE_STATE_DB={state_db}",
        f"--setenv=OBSIDIAN_VAULT_PATH={top / 'vault'}",
        f"--setenv=HIVE_ENV_FILE={top / '.env'}",
        f"--setenv=PYTHONPATH={top / 'host-lib'}",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        sys.executable, "-B", "-m", "hive.core.release_launcher",
        "--release-root", str(root), "--state-db", str(state_db),
        "--host-key", "auto", "--scope", "system",
        "--selection-credential", "selected_release",
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=60,
                          check=False)


def test_real_systemd_launches_only_the_host_selected_release():
    import pwd

    nobody = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory(prefix="hive-m56-systemd-") as scratch:
        top = Path(scratch)
        top.chmod(0o755)
        root, state_db, selector, first, second = _installed_releases(top)
        host_library = _install_host_launcher(top)
        assert host_library.stat().st_uid == 0
        os.chown(state_db.parent, nobody.pw_uid, nobody.pw_gid)
        for path in state_db.parent.iterdir():
            os.chown(path, nobody.pw_uid, nobody.pw_gid)
        credential_dir = top / "host-etc"
        credential_dir.mkdir(mode=0o755)
        credential = credential_dir / "selected-release"
        publish_selected_release(selector, credential, expected_sha=first)

        first_run = _run_service(top, root, state_db, credential)
        assert first_run.returncode == 0, first_run.stderr
        assert f"release-one uid={nobody.pw_uid}" in first_run.stdout

        selector.select_candidate(second, expected_selected=first)
        mismatch = _run_service(top, root, state_db, credential)
        assert mismatch.returncode != 0
        assert "release-two" not in mismatch.stdout

        publish_selected_release(selector, credential, expected_sha=second)
        second_run = _run_service(top, root, state_db, credential)
        assert second_run.returncode == 0, second_run.stderr
        assert f"release-two uid={nobody.pw_uid}" in second_run.stdout
