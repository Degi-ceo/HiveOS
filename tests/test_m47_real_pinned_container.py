"""Opt-in real-Docker smoke for the pinned host diagnostic transport.

The ordinary suite skips this test when a digest-pinned image is not supplied.
The dedicated Linux CI job must supply one and must not treat a skip as proof.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

from hive.core.candidate_evidence import _CHECKS
from hive.core.child_env import minimal_worker_environment
from hive.core.pinned_evidence_docker import DockerPinnedEvidenceRunner


@pytest.mark.skipif(
    not os.getenv("HIVE_TEST_PINNED_IMAGE"),
    reason="requires a pre-pulled digest-pinned image and Docker daemon",
)
def test_real_container_compiles_read_only_candidate_without_host_artifacts(tmp_path: Path):
    image = os.environ["HIVE_TEST_PINNED_IMAGE"]
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*@sha256:[0-9a-f]{64}", image)

    repo = tmp_path / "repo"
    checkout = repo / ".worktrees" / ("hive-supervisor-evidence-" + "a" * 32)
    source = checkout / "src" / "hive" / "candidate.py"
    source.parent.mkdir(parents=True)
    bad_source = "def broken(:\n"
    good_source = "answer = 42\n"
    source.write_text(bad_source, encoding="utf-8")

    runner = DockerPinnedEvidenceRunner(image=image, repo_root=repo, timeout_seconds=90)
    bad_result = asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[1][1]))
    assert bad_result[0] != 0
    assert bad_result[1] == ""
    assert source.read_text(encoding="utf-8") == bad_source

    source.write_text(good_source, encoding="utf-8")
    good_result = asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[1][1]))
    assert good_result == (0, "")
    assert source.read_text(encoding="utf-8") == good_source
    assert set(checkout.rglob("*")) == {checkout / "src", checkout / "src" / "hive", source}


@pytest.mark.skipif(
    not os.getenv("HIVE_TEST_FEEDBACK_IMAGE"),
    reason="requires the digest-pinned feedback image with pytest installed",
)
def test_real_container_rejects_host_credentials_network_and_checkout_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    image = os.environ["HIVE_TEST_PINNED_IMAGE"]
    repo = tmp_path / "repo"
    checkout = repo / ".worktrees" / ("hive-supervisor-evidence-" + "b" * 32)
    source = checkout / "src" / "hive" / "candidate.py"
    source.parent.mkdir(parents=True)
    source.write_text("answer = 42\n", encoding="utf-8")
    source.chmod(0o666)
    launch_env = minimal_worker_environment()
    network = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        check=True, capture_output=True, text=True, env=launch_env, timeout=30,
    )
    gateway = network.stdout.strip()
    assert gateway
    with socket.socket() as listener:
        listener.bind((gateway, 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        connect_probe = (
            "import socket; "
            f"socket.create_connection(({gateway!r}, {port}), timeout=2).close()"
        )
        reachable = subprocess.run(
            ["docker", "run", "--rm", "--pull=never", "--network=bridge",
             "--entrypoint=python", image, "-c", connect_probe],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=launch_env, timeout=30,
        )
        assert reachable.returncode == 0, "network positive control was unreachable"
        writable = subprocess.run(
            ["docker", "run", "--rm", "--pull=never", "--network=none",
             "--user=65534:65534", "--mount", f"type=bind,src={checkout},dst=/repo",
             "--entrypoint=python", image, "-c",
             "from pathlib import Path; Path('/repo/src/hive/candidate.py').write_text('control')"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=launch_env, timeout=30,
        )
        assert writable.returncode == 0, "write positive control was blocked"
        assert source.read_text(encoding="utf-8") == "control"
        source.write_text("answer = 42\n", encoding="utf-8")
        test_file = checkout / "tests" / "test_isolation.py"
        test_file.parent.mkdir()
        test_file.write_text(
            "import os\n"
            "import socket\n"
            "from pathlib import Path\n"
            "import pytest\n\n"
            "def test_isolation():\n"
            "    assert 'HIVE_APPROVER_KEY' not in os.environ\n"
            "    assert 'HIVE_TEST_CANARY' not in os.environ\n"
            "    with socket.socket() as connection:\n"
            "        connection.settimeout(2)\n"
            f"        assert connection.connect_ex(({gateway!r}, {port})) != 0\n"
            "    with pytest.raises(OSError):\n"
            "        Path('/repo/src/hive/candidate.py').write_text('changed')\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HIVE_APPROVER_KEY", "synthetic-m65-canary")
        monkeypatch.setenv("HIVE_TEST_CANARY", "synthetic-m65-canary")
        runner = DockerPinnedEvidenceRunner(image=image, repo_root=repo, timeout_seconds=90)
        assert asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[2][1])) == (0, "")
        assert source.read_text(encoding="utf-8") == "answer = 42\n"
