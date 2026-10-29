"""Run the actual selected HiveOS gateway in a transient Linux systemd unit."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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


def _run(*args: str, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _installed_gateway(top: Path) -> tuple[Path, Path, Path, str]:
    import pwd

    checkout = Path(__file__).resolve().parents[1]
    sha = _run("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert len(sha) in (40, 64)
    source = top / "source"
    cloned = _run("git", "clone", "-q", "--no-hardlinks", "--no-checkout",
                  str(checkout), str(source), timeout=60)
    assert cloned.returncode == 0, cloned.stderr[:300]
    root = top / "installed"
    (root / "releases").mkdir(parents=True)
    added = _run("git", "worktree", "add", "--detach", "-q",
                 str(root / "releases" / sha), sha, cwd=source, timeout=60)
    assert added.returncode == 0, added.stderr[:300]
    host_lib = top / "host-lib"
    shutil.copytree(checkout / "src" / "hive", host_lib / "hive")
    data = top / "data"
    data.mkdir()
    state_db = data / "state.sqlite"
    selector = InstalledReleaseSelector(
        root, DeployLedger(state_db), host_key=deployment_host_key(root), scope="system",
    )
    selector.select_candidate(sha, expected_selected=None)
    credential_dir = top / "host-etc"
    credential_dir.mkdir()
    credential = credential_dir / "selected-release"
    publish_selected_release(selector, credential, expected_sha=sha)
    nobody = pwd.getpwnam("nobody")
    for path in (data, *data.iterdir()):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    vault = top / "vault"
    vault.mkdir()
    os.chown(vault, nobody.pw_uid, nobody.pw_gid)
    return root, state_db, credential, sha


def _request(port: int, path: str, token: str = "") -> tuple[int, dict]:
    headers = {"X-Hive-Token": token} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def test_selected_full_gateway_serves_verified_revision_under_systemd():
    import pwd

    nobody = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory(prefix="hive-m57-gateway-") as scratch:
        top = Path(scratch)
        top.chmod(0o755)
        root, state_db, credential, sha = _installed_gateway(top)
        port = _port()
        unit = f"hiveos-m57-{uuid.uuid4().hex}.service"
        agent_token = "m57-agent-test-token-unique"
        approver_token = "m57-approver-test-token-unique"
        command = [
            "systemd-run", "--system", "--quiet", f"--unit={unit}",
            "--property=Type=exec", "--property=User=nobody",
            "--property=RuntimeMaxSec=60s", "--property=TimeoutStopSec=5s",
            f"--property=WorkingDirectory={root}",
            f"--property=LoadCredential=selected_release:{credential}",
            f"--setenv=HIVE_DATA_DIR={top / 'data'}",
            f"--setenv=HIVE_STATE_DB={state_db}",
            f"--setenv=OBSIDIAN_VAULT_PATH={top / 'vault'}",
            f"--setenv=HIVE_ENV_FILE={top / '.env'}",
            f"--setenv=PYTHONPATH={top / 'host-lib'}",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "--setenv=HIVE_HOST=127.0.0.1", f"--setenv=HIVE_PORT={port}",
            "--setenv=HIVE_AUTONOMY_ENABLED=true",
            "--setenv=HIVE_DEPLOY_VERIFY_ENABLED=true",
            "--setenv=HIVE_WORKER_ISOLATION=required",
            f"--setenv=HIVE_SECRET={agent_token}",
            f"--setenv=HIVE_APPROVER_KEY={approver_token}",
            sys.executable, "-B", "-m", "hive.core.release_launcher",
            "--release-root", str(root), "--state-db", str(state_db),
            "--host-key", "auto", "--scope", "system",
            "--selection-credential", "selected_release",
        ]
        try:
            started = _run(*command, timeout=20)
            assert started.returncode == 0, started.stderr[:300]
            deadline = time.monotonic() + 30
            health = None
            while time.monotonic() < deadline:
                try:
                    status, health = _request(port, "/health")
                    if status == 200:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
            assert health is not None and status == 200, (
                "managed gateway did not become healthy; "
                + _run("journalctl", "-u", unit, "--no-pager", "-n", "30").stdout[-3000:]
            )
            assert health["service"] == "hiveos-gateway"
            assert health["source_revision"] == sha
            pid = int(health["process_pid"])
            main_pid = _run("systemctl", "show", unit, "--property=MainPID", "--value")
            assert main_pid.returncode == 0
            assert int(main_pid.stdout.strip()) == pid
            assert Path(f"/proc/{pid}").stat().st_uid == nobody.pw_uid
            assert _request(port, "/health/full")[0] == 401
            full_status, full = _request(port, "/health/full", agent_token)
            assert full_status == 200
            assert full["source_revision"] == sha
            print(f"systemd full gateway: health ok, revision={sha}, service PID={pid}, uid={nobody.pw_uid}")
            print("systemd full gateway: unauthenticated full health refused; agent-authenticated health passed")
        finally:
            _run("systemctl", "stop", unit, timeout=15)
            active = _run("systemctl", "is-active", "--quiet", unit, timeout=10)
            if active.returncode == 0:
                raise RuntimeError("M57 transient gateway remained active after cleanup")
