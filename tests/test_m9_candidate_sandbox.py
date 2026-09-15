import asyncio

import pytest

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


def test_candidate_runner_output_is_suppressed(tmp_path):
    candidate = tmp_path / "candidate"
    (candidate / "tests").mkdir(parents=True)

    async def noisy(_argv):
        return 1, "secret output"

    result = asyncio.run(CandidateContainerRunner("python:3.12", run=noisy).run(
        str(candidate), ["ruff", "check", "tests/"]))
    assert result == (1, "secret output")
