"""M12: Docker worker boundary is explicit, pinned, and fail-closed."""
from __future__ import annotations

from dataclasses import replace
import asyncio

import pytest

from hive.agents.worker_process import WorkerContainmentUnavailable, WorkerProcessController
from hive.agents.worker_sandbox import DockerWorkerSandbox, WorkerSandboxUnavailable, worker_sandbox_capability
from hive.core.config import HiveConfig
from hive.runtime import HiveOS


_IMAGE = "registry.example/hive-worker@sha256:" + "a" * 64


def test_worker_sandbox_requires_a_digest_pinned_image(monkeypatch, tmp_path):
    monkeypatch.setattr("hive.core.worker_sandbox.shutil.which", lambda _name: "docker")
    assert not worker_sandbox_capability("python:3.12").available
    with pytest.raises(WorkerSandboxUnavailable, match="digest-pinned"):
        DockerWorkerSandbox("python:3.12", tmp_path)


def test_worker_sandbox_builds_a_networkless_readonly_command(monkeypatch, tmp_path):
    monkeypatch.setattr("hive.core.worker_sandbox.shutil.which", lambda _name: "docker")
    command = DockerWorkerSandbox(_IMAGE, tmp_path).wrap((
        "C:/Python/python.exe", "-I", "-c", "worker-entry", str(tmp_path),
    ))

    assert command[:7] == ("docker", "run", "--rm", "--name", command[4], "--interactive", "--pull")
    for required in ("never", "none", "--read-only", "ALL", "no-new-privileges", "65534:65534"):
        assert required in command
    assert f"{tmp_path.resolve()}:/app:ro" in command
    assert "--volume" in command and "--network" in command
    assert "/app" == command[-1]
    assert "docker.sock" not in " ".join(command).casefold()


def test_required_worker_sandbox_never_constructs_a_local_fallback():
    with pytest.raises(WorkerContainmentUnavailable, match="required worker sandbox"):
        WorkerProcessController("preferred", sandbox_mode="required")


def test_worker_sandbox_force_removes_named_container_after_client_loss(monkeypatch, tmp_path):
    monkeypatch.setattr("hive.core.worker_sandbox.shutil.which", lambda _name: "docker")
    sandbox = DockerWorkerSandbox(_IMAGE, tmp_path)
    command = sandbox.wrap(("python", "-c", "pass", str(tmp_path)))
    seen: list[tuple[str, ...]] = []

    class _Proc:
        async def wait(self):
            return 0

    async def fake_start(*args, **_kwargs):
        seen.append(args)
        return _Proc()

    monkeypatch.setattr("hive.agents.worker_sandbox.asyncio.create_subprocess_exec", fake_start)
    asyncio.run(sandbox.cleanup())

    assert seen == [("docker", "rm", "-f", command[4])]
    assert sandbox.container_name == ""


def test_config_validates_required_worker_sandbox(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_WORKER_SANDBOX_IMAGE", raising=False)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    required = replace(cfg, worker_sandbox="required")
    assert "HIVE_WORKER_SANDBOX=required requires HIVE_WORKER_SANDBOX_IMAGE" in required.validate()
    valid = replace(required, worker_sandbox_image=_IMAGE)
    assert not any("HIVE_WORKER_SANDBOX" in item for item in valid.validate())


def test_runtime_capability_checks_daemon_and_local_image(monkeypatch):
    monkeypatch.setattr("hive.core.worker_sandbox.shutil.which", lambda _name: "docker")

    class _Result:
        returncode = 1

    monkeypatch.setattr("hive.core.worker_sandbox.subprocess.run", lambda *_args, **_kwargs: _Result())
    capability = worker_sandbox_capability(_IMAGE, verify_runtime=True)
    assert not capability.available
    assert capability.detail == "Docker daemon is unavailable"


def test_required_sandbox_build_fails_closed_when_preflight_fails(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        worker_sandbox="required", worker_sandbox_image=_IMAGE,
    )
    monkeypatch.setattr(
        "hive.core.worker_sandbox.worker_sandbox_capability",
        lambda *_args, **_kwargs: type("Capability", (), {"available": False, "detail": "image missing"})(),
    )
    with pytest.raises(RuntimeError, match="image missing"):
        HiveOS.build(cfg, router=object())


def test_worker_import_does_not_load_parent_approval_executor() -> None:
    """The mounted worker source must not require the protected Core tree."""
    import sys

    prefixes = ("hive.agents.worker", "hive.tools.executor")
    saved = {name: module for name, module in sys.modules.items()
             if name == prefixes[0] or name.startswith(prefixes[1])}
    try:
        for name in saved:
            sys.modules.pop(name, None)
        __import__("hive.agents.worker")
        assert "hive.tools.executor" not in sys.modules
    finally:
        for name in tuple(sys.modules):
            if name == prefixes[0] or name.startswith(prefixes[1]):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
