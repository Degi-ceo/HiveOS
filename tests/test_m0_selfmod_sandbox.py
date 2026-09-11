"""M0 issue #121: autonomous self-modification must use a sandbox."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from hive.core.config import HiveConfig
from hive.core.sandbox import make_sandbox_runner
from hive.runtime import HiveOS


class _Router:
    async def complete(self, *args, **kwargs):  # pragma: no cover - build-only stub
        raise AssertionError("the router must not be called during construction")

    async def aclose(self):
        pass


def _autonomous_config(tmp_path, *, sandbox_image: str) -> HiveConfig:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return replace(
        cfg,
        autonomy_enabled=True,
        autonomous_selfmod_enabled=True,
        approver_key="test-approver-key",
        sandbox_image=sandbox_image,
    )


def test_autonomous_selfmod_startup_requires_sandbox_image(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")

    with pytest.raises(RuntimeError, match="HIVE_SANDBOX_IMAGE"):
        HiveOS.build(cfg, router=_Router())


def test_config_validation_reports_missing_autonomous_selfmod_sandbox(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")

    assert any("HIVE_SANDBOX_IMAGE" in issue for issue in cfg.validate())


def test_learning_loop_startup_requires_sandbox_image(tmp_path):
    cfg = replace(
        _autonomous_config(tmp_path, sandbox_image="python:3.12"),
        autonomous_selfmod_enabled=False,
        learning_loop_enabled=True,
        sandbox_image="",
    )

    with pytest.raises(RuntimeError, match="HIVE_LEARNING_LOOP_ENABLED"):
        HiveOS.build(cfg, router=_Router())


def test_config_validation_reports_missing_learning_sandbox(tmp_path):
    cfg = replace(
        _autonomous_config(tmp_path, sandbox_image="python:3.12"),
        autonomous_selfmod_enabled=False,
        learning_loop_enabled=True,
        sandbox_image="",
    )

    assert any("HIVE_LEARNING_LOOP_ENABLED" in issue for issue in cfg.validate())


def test_learning_regression_threshold_is_configurable_and_validated(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HIVE_LEARNING_REGRESSION_THRESHOLD", "0.05")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.learning_regression_threshold == 0.05
    assert not any("HIVE_LEARNING_REGRESSION_THRESHOLD" in issue for issue in cfg.validate())
    invalid = replace(cfg, learning_regression_threshold=1.01)
    assert any("HIVE_LEARNING_REGRESSION_THRESHOLD" in issue for issue in invalid.validate())
    with pytest.raises(RuntimeError, match="HIVE_LEARNING_REGRESSION_THRESHOLD"):
        HiveOS.build(invalid, router=_Router())


def test_supervised_selfmod_remains_available_without_sandbox(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")
    cfg = replace(cfg, autonomous_selfmod_enabled=False)

    hive = HiveOS.build(cfg, router=_Router())

    assert hive.self_modifier is not None


def test_autonomous_build_wires_the_configured_sandbox_runner(tmp_path, monkeypatch):
    captured = {}

    async def sandbox_runner(cmd, cwd=None):
        return 0, "ok"

    def fake_make_sandbox_runner(image, *, repo_root):
        captured["image"] = image
        captured["repo_root"] = repo_root
        return sandbox_runner

    monkeypatch.setattr("hive.runtime.make_sandbox_runner", fake_make_sandbox_runner)
    cfg = _autonomous_config(tmp_path, sandbox_image="python:3.12")

    hive = HiveOS.build(cfg, router=_Router())

    assert captured == {"image": "python:3.12", "repo_root": str(tmp_path)}
    assert hive.self_modifier._run is sandbox_runner


def test_sandbox_runner_routes_candidate_test_command_through_docker():
    seen = []

    async def local(cmd, cwd=None):
        seen.append((cmd, cwd))
        return 0, "ok"

    runner = make_sandbox_runner("python:3.12", repo_root="/candidate", base=local)
    asyncio.run(runner("python -m pytest -q", "/candidate"))

    command, cwd = seen[0]
    assert cwd == "/candidate"
    assert command.startswith("docker run --rm --network none")
    assert "-v /candidate:/repo" in command
    assert "python -m pytest -q" in command


def test_sandbox_runner_force_removes_container_when_cancelled():
    seen = []
    started = asyncio.Event()

    async def local(cmd, cwd=None):
        seen.append((cmd, cwd))
        if isinstance(cmd, str):
            started.set()
            await asyncio.Future()
        return 0, "removed"

    async def drive():
        runner = make_sandbox_runner("python:3.12", repo_root="/candidate", base=local)
        task = asyncio.create_task(runner("python -m pytest -q", "/candidate"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    cleanup = [cmd for cmd, _cwd in seen if isinstance(cmd, list)]
    assert len(cleanup) == 1
    assert cleanup[0][:3] == ["docker", "rm", "-f"]
    assert cleanup[0][3].startswith("hive-sandbox-")


def test_sandbox_cleanup_has_its_own_timeout(monkeypatch):
    started = asyncio.Event()

    async def local(cmd, cwd=None):  # noqa: ARG001
        if isinstance(cmd, str):
            started.set()
        await asyncio.Future()

    async def drive():
        runner = make_sandbox_runner("python:3.12", repo_root="/candidate", base=local)
        task = asyncio.create_task(runner("python -m pytest -q", "/candidate"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)

    monkeypatch.setattr("hive.core.sandbox._CONTAINER_CLEANUP_TIMEOUT", 0.01)
    asyncio.run(drive())
