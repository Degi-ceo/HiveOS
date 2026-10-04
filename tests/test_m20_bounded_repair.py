"""Real-Git regressions for one-candidate bounded self-mod repair."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from hive.core.self_mod import SelfModifier, _default_run


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _seed_repo(tmp_path: Path, *, name: str = "candidate-repo") -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hive Test")
    _git(repo, "config", "user.email", "hive-test@localhost")
    (repo / "demo.py").write_text("value = 0\n", encoding="utf-8")
    (repo / "test_demo.py").write_text(
        "from demo import value\n\ndef test_value():\n    assert value == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_two_step_repair_reuses_candidate_and_gates_final_tree(tmp_path):
    repo, base = _seed_repo(tmp_path, name="candidate repo & safe argv")
    test_cmd = f'"{sys.executable}" -m pytest -q test_demo.py'
    commands: list[str] = []
    worktrees: list[str] = []
    gate_calls: list[tuple[str, str, list[str], str, str]] = []

    async def run(command, cwd=None):
        commands.append(" ".join(command) if isinstance(command, list) else command)
        return await _default_run(command, cwd)

    async def initial(worktree):
        worktrees.append(worktree)
        (Path(worktree) / "demo.py").write_text("value = 1\n", encoding="utf-8")
        return ["demo.py"]

    async def repair(failure):
        assert failure.attempt == 1
        assert "assert 1 == 2" in failure.test_log
        assert "+value = 1" in failure.staged_diff
        assert failure.run_id == "m20-two-step"

        async def apply(worktree):
            worktrees.append(worktree)
            (Path(worktree) / "demo.py").write_text("value = 2\n", encoding="utf-8")
            return ["demo.py"]

        return apply

    async def gate(worktree, last_good, changed, run_id, digest):
        gate_calls.append((worktree, last_good, changed, run_id, digest))
        assert (Path(worktree) / "demo.py").read_text(encoding="utf-8") == "value = 2\n"
        return {"ok": True, "candidate_digest": digest}

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), run=run, test_cmd=test_cmd,
    ).propose(
        "candidate", "", initial, dry_run=True, run_id="m20-two-step",
        repair_fn=repair, max_repair_attempts=2, candidate_gate=gate,
    ))

    assert result["ok"] is True and result["repair_attempts"] == 1
    assert worktrees == [worktrees[0], worktrees[0]]
    assert sum(command.startswith("git worktree add -b ") for command in commands) == 1
    assert commands.count(test_cmd) == 2
    assert len(gate_calls) == 1
    assert gate_calls[0][1:4] == (base, ["demo.py"], "m20-two-step")
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _git(repo, "branch", "--list", "hive/auto-*")


def test_repair_delta_with_secret_is_blocked_before_second_test(tmp_path):
    repo, base = _seed_repo(tmp_path)
    token = "ghp_" + "a" * 36
    test_cmd = f'"{sys.executable}" -m pytest -q test_demo.py'
    commands: list[str] = []

    async def run(command, cwd=None):
        commands.append(" ".join(command) if isinstance(command, list) else command)
        return await _default_run(command, cwd)

    async def initial(worktree):
        (Path(worktree) / "demo.py").write_text("value = 1\n", encoding="utf-8")
        return ["demo.py"]

    async def repair(_failure):
        async def apply(worktree):
            (Path(worktree) / "demo.py").write_text(
                "value = 2\nkey = '" + token + "'\n", encoding="utf-8",
            )
            return ["demo.py"]

        return apply

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), run=run, test_cmd=test_cmd,
    ).propose(
        "candidate", "", initial, dry_run=True, repair_fn=repair,
        max_repair_attempts=2,
    ))

    assert result["stage"] == "secret_scan"
    assert result["repair_attempts"] == 1
    assert commands.count(test_cmd) == 1
    assert sum(command.startswith("git worktree add -b ") for command in commands) == 1
    assert token not in str(result)
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_repair_delta_with_unreported_path_is_blocked_before_second_test(tmp_path):
    repo, base = _seed_repo(tmp_path)
    test_cmd = f'"{sys.executable}" -m pytest -q test_demo.py'
    commands: list[str] = []

    async def run(command, cwd=None):
        commands.append(" ".join(command) if isinstance(command, list) else command)
        return await _default_run(command, cwd)

    async def initial(worktree):
        (Path(worktree) / "demo.py").write_text("value = 1\n", encoding="utf-8")
        return ["demo.py"]

    async def repair(_failure):
        async def apply(worktree):
            (Path(worktree) / "demo.py").write_text("value = 2\n", encoding="utf-8")
            (Path(worktree) / "extra.py").write_text("unexpected = True\n", encoding="utf-8")
            return ["demo.py"]

        return apply

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), run=run, test_cmd=test_cmd,
    ).propose(
        "candidate", "", initial, dry_run=True, repair_fn=repair,
        max_repair_attempts=2,
    ))

    assert result["stage"] == "changed_files"
    assert result["repair_attempts"] == 1
    assert commands.count(test_cmd) == 1
    assert sum(command.startswith("git worktree add -b ") for command in commands) == 1
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_three_failed_attempts_use_exactly_one_candidate_branch_and_worktree(tmp_path):
    repo, base = _seed_repo(tmp_path)
    test_cmd = f'"{sys.executable}" -m pytest -q test_demo.py'
    commands: list[str] = []
    worktrees: list[str] = []
    candidate_branches: list[str] = []
    repairs = 0

    async def run(command, cwd=None):
        commands.append(" ".join(command) if isinstance(command, list) else command)
        return await _default_run(command, cwd)

    async def change(worktree, value):
        worktrees.append(worktree)
        candidate_branches.append(_git(Path(worktree), "branch", "--show-current"))
        (Path(worktree) / "demo.py").write_text(f"value = {value}\n", encoding="utf-8")
        return ["demo.py"]

    async def initial(worktree):
        return await change(worktree, 1)

    async def repair(failure):
        nonlocal repairs
        repairs += 1
        assert failure.attempt == repairs

        async def apply(worktree):
            return await change(worktree, repairs + 2)

        return apply

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), run=run, test_cmd=test_cmd,
    ).propose(
        "candidate", "", initial, run_id="m20-three-attempts",
        repair_fn=repair, max_repair_attempts=99,
    ))

    assert result["stage"] == "repair_exhausted"
    assert result["repair_attempts"] == 2
    assert repairs == 2
    assert commands.count(test_cmd) == 3
    assert sum(command.startswith("git worktree add -b ") for command in commands) == 1
    assert len(worktrees) == 3 and len(set(worktrees)) == 1
    assert len(set(candidate_branches)) == 1
    assert candidate_branches[0].startswith("hive/auto-")
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not _git(repo, "branch", "--list", "hive/auto-*")
