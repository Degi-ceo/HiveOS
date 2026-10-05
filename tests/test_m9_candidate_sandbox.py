import asyncio

import pytest

from hive.agents import candidate_sandbox
from hive.agents.candidate_sandbox import CandidateContainerRunner, validate_candidate_argv


@pytest.mark.parametrize("argv", [
    "python -m pytest -q tests/x.py", ["bash", "-c", "id"],
    ["python", "-c", "import os"], ["python", "-m", "pip", "install", "x"],
    ["ruff", "check", "../src"], ["python", "-m", "pytest", "--config", "x", "tests/x.py"],
])
def test_candidate_commands_reject_shells_and_escape_hatches(argv):
    with pytest.raises(ValueError):
        validate_candidate_argv(argv)


def test_candidate_container_uses_exec_argv_and_locked_down_mount(tmp_path):
    candidate = tmp_path / "candidate"
    (candidate / "src" / "hive").mkdir(parents=True)
    seen = []

    async def fake_run(argv):
        seen.append(argv)
        return 0, "ok"

    runner = CandidateContainerRunner("python:3.12", run=fake_run)
    assert asyncio.run(runner.run(str(candidate), ["python", "-m", "compileall", "src/hive"])) == (0, "ok")
    command = seen[0]
    assert command[:4] == ["docker", "run", "--rm", "--name"]
    assert command[command.index("--network") + 1] == "none"
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert f"{candidate.resolve()}:/repo:ro" in command
    assert "sh" not in command and "bash" not in command


def test_candidate_container_never_falls_back_when_docker_is_unavailable(tmp_path):
    candidate = tmp_path / "candidate"
    (candidate / "tests").mkdir(parents=True)

    async def unavailable(_argv):
        raise FileNotFoundError("docker")

    result = asyncio.run(CandidateContainerRunner("python:3.12", run=unavailable).run(
        str(candidate), ["ruff", "check", "tests/"]))
    assert result == (126, "[candidate container unavailable]")


def test_pinned_evidence_requires_digest_image_and_disables_pulls(tmp_path):
    candidate = tmp_path / "candidate"
    (candidate / "src" / "hive").mkdir(parents=True)
    seen = []

    async def fake_run(argv):
        seen.append(argv)
        return 0, "suppressed"

    tagged = CandidateContainerRunner("python:3.12", run=fake_run)
    with pytest.raises(ValueError, match="pinned"):
        asyncio.run(tagged.run_pinned_evidence(
            str(candidate), ["python", "-m", "compileall", "src/hive"],
        ))

    digest = "a" * 64
    pinned = CandidateContainerRunner(f"python:3.12@sha256:{digest}", run=fake_run)
    assert asyncio.run(pinned.run_pinned_evidence(
        str(candidate), ["python", "-m", "compileall", "src/hive"],
    )) == (0, "suppressed")
    assert pinned.pinned_image_digest == f"sha256:{digest}"
    assert seen[-1][2:4] == ["--pull", "never"]


def test_cancelled_default_runner_reaps_named_docker_container(monkeypatch):
    created = []

    class _Process:
        def __init__(self, *, completes_on_kill=False):
            self.stdout = self.stderr = None
            self.returncode = None
            self._done = asyncio.Event()
            self._completes_on_kill = completes_on_kill

        async def wait(self):
            await self._done.wait()
            return self.returncode

        def kill(self):
            self.returncode = -9
            self._done.set()

    command = _Process(completes_on_kill=True)
    cleanup = _Process()
    cleanup.returncode = 0
    cleanup._done.set()

    async def fake_exec(*argv, **_kwargs):
        created.append(argv)
        return cleanup if argv[:3] == ("docker", "rm", "-f") else command

    monkeypatch.setattr(candidate_sandbox.asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        task = asyncio.create_task(candidate_sandbox._default_run([
            "docker", "run", "--name", "hive-candidate-test", "image",
        ]))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ("docker", "rm", "-f", "hive-candidate-test") in created


def test_candidate_runner_output_is_suppressed(tmp_path):
    candidate = tmp_path / "candidate"
    (candidate / "tests").mkdir(parents=True)

    async def noisy(_argv):
        return 1, "secret output"

    result = asyncio.run(CandidateContainerRunner("python:3.12", run=noisy).run(
        str(candidate), ["ruff", "check", "tests/"]))
    assert result == (1, "secret output")
