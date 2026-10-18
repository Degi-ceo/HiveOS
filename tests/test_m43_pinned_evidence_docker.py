"""Host-side Docker transport for fixed M36 candidate diagnostics."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from hive.core.candidate_evidence import CandidateEvidenceIssuer, _CHECKS
from hive.core.pinned_evidence_docker import DockerPinnedEvidenceRunner, _run_command
from tests.test_m36_candidate_evidence import _Git, _binding

IMAGE = "ghcr.io/example/hive-evidence@sha256:" + "a" * 64


def _checkout(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / ".worktrees" / ("hive-supervisor-evidence-" + "b" * 32)
    checkout.mkdir(parents=True)
    return repo, checkout


def test_fixed_diagnostic_uses_pinned_read_only_isolated_container(tmp_path, monkeypatch):
    repo, checkout = _checkout(tmp_path)
    calls = []

    async def fake_run(argv, cwd, timeout):
        calls.append((argv, cwd, timeout))
        return 0, "untrusted diagnostic output"

    monkeypatch.setattr("hive.core.pinned_evidence_docker._run_command", fake_run)
    runner = DockerPinnedEvidenceRunner(image=IMAGE, repo_root=repo)
    assert runner.pinned_image_digest == "sha256:" + "a" * 64
    assert asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[0][1])) == (0, "")
    argv, cwd, timeout = calls[0]
    assert argv[:2] == ["docker", "run"]
    assert "--pull=never" in argv
    assert "--no-healthcheck" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--user=65534:65534" in argv
    assert f"type=bind,src={checkout},dst=/repo,readonly" in argv
    assert "--entrypoint=ruff" in argv
    assert argv[-3:] == [IMAGE, "check", "src/hive"]
    assert cwd == str(repo)
    assert timeout > 0


def test_rejects_unpinned_image_and_non_evidence_worktree(tmp_path, monkeypatch):
    repo, checkout = _checkout(tmp_path)
    with pytest.raises(ValueError):
        DockerPinnedEvidenceRunner(image="python:3.12", repo_root=repo)
    calls = []

    async def fake_run(argv, cwd, timeout):
        calls.append(argv)
        return 0, ""

    monkeypatch.setattr("hive.core.pinned_evidence_docker._run_command", fake_run)
    runner = DockerPinnedEvidenceRunner(image=IMAGE, repo_root=repo)
    assert asyncio.run(runner.run_pinned_evidence(str(checkout), ("sh", "-c", "id"))) == (1, "")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert asyncio.run(runner.run_pinned_evidence(str(outside), _CHECKS[0][1])) == (1, "")
    assert calls == []


def test_issuer_persists_only_fixed_success_codes_from_docker_runner(tmp_path, monkeypatch):
    repo, checkout = _checkout(tmp_path)
    commands = []

    async def fake_run(argv, cwd, timeout):
        commands.append(argv)
        return 0, "secret-bearing candidate output"

    monkeypatch.setattr("hive.core.pinned_evidence_docker._run_command", fake_run)
    runner = DockerPinnedEvidenceRunner(image=IMAGE, repo_root=repo)
    binding = replace(_binding(), image_digest=runner.pinned_image_digest)
    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", runner, git_run=_Git())
    receipt = asyncio.run(issuer.issue(binding, str(checkout)))
    assert receipt is not None
    assert receipt.check_results == (("ruff", 0), ("compileall", 0), ("pytest", 0))
    assert len(commands) == 3
    assert [next(arg for arg in cmd if arg.startswith("--entrypoint=")) for cmd in commands] == [
        "--entrypoint=ruff", "--entrypoint=python", "--entrypoint=python",
    ]
    assert "secret-bearing" not in str(issuer.receipt(binding))


def test_timeout_attempts_one_container_cleanup(tmp_path, monkeypatch):
    repo, checkout = _checkout(tmp_path)
    calls = []

    async def fake_run(argv, cwd, timeout):
        calls.append(argv)
        if argv[1] == "run":
            raise TimeoutError
        return 0, ""

    monkeypatch.setattr("hive.core.pinned_evidence_docker._run_command", fake_run)
    runner = DockerPinnedEvidenceRunner(image=IMAGE, repo_root=repo)
    assert asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[1][1])) == (1, "")
    assert [argv[1] for argv in calls] == ["run", "rm"]
    assert calls[1][-1] == calls[0][calls[0].index("--name") + 1]


def test_cancellation_attempts_cleanup_and_does_not_issue_success(tmp_path, monkeypatch):
    repo, checkout = _checkout(tmp_path)
    calls = []

    async def fake_run(argv, cwd, timeout):
        calls.append(argv)
        if argv[1] == "run":
            raise asyncio.CancelledError
        return 0, ""

    monkeypatch.setattr("hive.core.pinned_evidence_docker._run_command", fake_run)
    runner = DockerPinnedEvidenceRunner(image=IMAGE, repo_root=repo)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[2][1]))
    assert [argv[1] for argv in calls] == ["run", "rm"]


def test_docker_cli_child_environment_excludes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "should-not-pass")
    monkeypatch.setenv("HIVE_GITHUB_TOKEN", "should-not-pass")
    monkeypatch.setenv("OTHER_SECRET", "should-not-pass")
    seen = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return None, None

    async def fake_exec(*argv, **kwargs):
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(_run_command(["docker", "version"], str(tmp_path), 1)) == (0, "")
    assert "HIVE_APPROVER_KEY" not in seen["env"]
    assert "HIVE_GITHUB_TOKEN" not in seen["env"]
    assert "OTHER_SECRET" not in seen["env"]
