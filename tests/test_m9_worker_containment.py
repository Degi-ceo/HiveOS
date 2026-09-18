"""M9.7: local workers are contained as process trees, not lone children."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hive.agents import worker_process
from hive.agents.worker_process import (
    WorkerContainmentUnavailable,
    WorkerIsolationCapability,
    WorkerProcessController,
    worker_isolation_capability,
)
from hive.core.config import HiveConfig
from hive.core import worker_isolation
from hive.runtime import HiveOS


async def _wait_for(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        await asyncio.sleep(0.02)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def _wait_for_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_exists(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"contained descendant {pid} survived termination")
        await asyncio.sleep(0.05)


@pytest.mark.skipif(
    not worker_isolation_capability().available,
    reason="host has no process-tree containment backend",
)
def test_required_containment_stops_a_real_descendant_process(tmp_path):
    """The platform backend must kill an actual grandchild, not just its parent."""
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    pid_file = tmp_path / "grandchild.pid"
    child = """
import pathlib, subprocess, sys, time
ready, release, pid_file = map(pathlib.Path, sys.argv[1:])
ready.write_text('ready', encoding='utf-8')
while not release.exists():
    time.sleep(0.01)
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pid_file.write_text(str(grandchild.pid), encoding='utf-8')
time.sleep(60)
"""

    async def scenario() -> int:
        controller = WorkerProcessController("required")
        proc = await controller.start(
            sys.executable, "-c", child, str(ready), str(release), str(pid_file),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _wait_for(ready)
        release.write_text("go", encoding="utf-8")
        await _wait_for(pid_file)
        descendant = int(pid_file.read_text(encoding="utf-8"))
        assert _pid_exists(descendant)
        await controller.stop(proc)
        await controller.stop(proc)  # cancellation is idempotent
        return descendant

    descendant = asyncio.run(scenario())
    asyncio.run(_wait_for_exit(descendant))


def test_required_mode_refuses_to_launch_without_containment(monkeypatch):
    monkeypatch.setattr(
        worker_process, "worker_isolation_capability",
        lambda: WorkerIsolationCapability(False, "unavailable", "none", "test backend unavailable"),
    )

    async def scenario() -> None:
        controller = WorkerProcessController("required")
        with pytest.raises(WorkerContainmentUnavailable, match="test backend unavailable"):
            await controller.start(sys.executable, "-c", "raise SystemExit(0)")

    asyncio.run(scenario())


def test_preferred_mode_exposes_bounded_fallback_without_claiming_strong(monkeypatch):
    monkeypatch.setattr(
        worker_process, "worker_isolation_capability",
        lambda: WorkerIsolationCapability(False, "unavailable", "none", "test backend unavailable"),
    )
    controller = WorkerProcessController("preferred")
    assert controller.effective_level == "bounded"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object attach path")
def test_required_windows_attach_failure_kills_uncontained_child(monkeypatch):
    """A failed Job attachment must reap the spawned child before failing closed."""
    seen_pids: list[int] = []

    class FailingJob:
        def assign(self, pid: int) -> None:
            seen_pids.append(pid)
            raise OSError("simulated assignment failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(worker_process, "_WindowsJob", FailingJob)

    async def scenario() -> None:
        controller = WorkerProcessController("required")
        with pytest.raises(WorkerContainmentUnavailable, match="could not attach"):
            await controller.start(sys.executable, "-c", "import time; time.sleep(60)")

    asyncio.run(scenario())
    assert seen_pids
    asyncio.run(_wait_for_exit(seen_pids[0]))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup path")
def test_required_containment_kills_descendant_after_leader_exits(tmp_path):
    """Cleanup must kill an orphaned group member after the worker leader exits."""
    pid_file = tmp_path / "orphan.pid"
    child = """
import pathlib, subprocess, sys
pid_file = pathlib.Path(sys.argv[1])
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pid_file.write_text(str(grandchild.pid), encoding='utf-8')
"""

    async def scenario() -> int:
        controller = WorkerProcessController("required")
        proc = await controller.start(
            sys.executable, "-c", child, str(pid_file),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _wait_for(pid_file)
        descendant = int(pid_file.read_text(encoding="utf-8"))
        await proc.wait()
        assert _pid_exists(descendant)
        await controller.stop(proc)
        return descendant

    descendant = asyncio.run(scenario())
    asyncio.run(_wait_for_exit(descendant))


def test_config_requires_required_isolation_for_autonomy(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    autonomous = replace(cfg, autonomy_enabled=True, approver_key="approver", worker_isolation="preferred")
    assert "HIVE_AUTONOMY_ENABLED=true requires HIVE_WORKER_ISOLATION=required" in autonomous.validate()
    assert replace(autonomous, worker_isolation="required").worker_isolation == "required"


def test_autonomy_build_fails_closed_when_containment_backend_is_missing(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="approver", worker_isolation="required",
    )
    monkeypatch.setattr(
        worker_isolation, "worker_isolation_capability",
        lambda: WorkerIsolationCapability(False, "unavailable", "none", "test backend unavailable"),
    )
    with pytest.raises(RuntimeError, match="available worker containment backend"):
        HiveOS.build(cfg, router=object())


def test_safe_config_reports_policy_not_credentials(tmp_path):
    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), approver_key="approver-secret")
    safe = cfg.to_safe_dict()
    assert safe["worker_isolation"] == "preferred"
    assert safe["approver_key"] == "***"
    assert "approver-secret" not in repr(safe)
