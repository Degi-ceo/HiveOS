"""M4 secret scan — candidate safety reports must never reveal a secret value."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from hive.core.secret_scan import scan_added_diff
from hive.core.self_mod import CandidateFailure, SelfModifier
from hive.core.spec_search import Edit, EditOp, SelfImprovement


def test_scanner_reports_known_secret_without_returning_value():
    token = "ghp_" + "a" * 36
    findings = scan_added_diff(
        "diff --git a/demo.py b/demo.py\n+++ b/demo.py\n@@ -0,0 +1 @@\n+token = '" + token + "'\n"
    )
    assert findings and findings[0].rule == "github_token"
    assert token not in str(findings)


def test_scanner_ignores_deleted_secret_and_safe_placeholder():
    token = "sk-" + "z" * 28
    findings = scan_added_diff(
        "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-API_KEY='" + token
        + "'\n+API_KEY='change_me'\n"
    )
    assert findings == []


def test_scanner_checks_added_content_that_begins_with_double_plus():
    token = "ghp_" + "a" * 36
    findings = scan_added_diff(
        "+++ b/demo.txt\n@@ -0,0 +1 @@\n+++" + token + "\n"
    )
    assert any(finding.rule == "github_token" for finding in findings)


def test_self_modifier_blocks_staged_secret_before_commit():
    token = "AKIA" + "A" * 16
    calls: list[str] = []

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        calls.append(text)
        if text == "git write-tree":
            return 0, "a" * 40 + "\n"
        if text == "git rev-parse HEAD^{tree}":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "src/demo.py\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text.startswith("git diff --cached"):
            return 0, "+++ b/src/demo.py\n@@ -0,0 +1 @@\n+key = '" + token + "'\n"
        return 0, "ok"

    async def apply(_worktree):
        return ["src/demo.py"]

    result = asyncio.run(SelfModifier(repo_root="/tmp/hive", run=run).propose("candidate", "", apply))
    assert result["stage"] == "secret_scan"
    assert token not in str(result)
    assert not any("commit-tree" in call for call in calls)
    assert not any("git commit" in call or call.startswith("git push") for call in calls)


def test_secret_scanner_failure_blocks_before_materialization():
    calls: list[str] = []

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        calls.append(text)
        if text == "git write-tree":
            return 0, "a" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "src/demo.py\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text.startswith("git diff --cached"):
            return 0, "+++ b/src/demo.py\n@@ -0,0 +1 @@\n+value = 1\n"
        return 0, "ok"

    async def apply(_worktree):
        return ["src/demo.py"]

    def broken_scanner(_diff):
        raise RuntimeError("secret value must not escape")

    result = asyncio.run(SelfModifier(
        repo_root="/tmp/hive", run=run, secret_scanner=broken_scanner,
    ).propose("candidate", "", apply))
    assert result["stage"] == "secret_scan_error"
    assert "secret value" not in str(result)
    assert not any("commit-tree" in call for call in calls)


def test_candidate_attributes_cannot_hide_staged_secret(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Hive Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "hive-test@localhost"], cwd=repo, check=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    async def apply(worktree):
        root = Path(worktree)
        (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
        (root / "hidden.py").write_text(
            "value = 'AKIA" + "A" * 16 + "'\n", encoding="utf-8",
        )
        return [".gitattributes", "hidden.py"]

    result = asyncio.run(SelfModifier(repo_root=str(repo)).propose(
        "candidate", "", apply, dry_run=True,
    ))
    assert result["stage"] == "secret_scan"
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == base
    assert subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.count("worktree ") == 1


def test_self_modifier_repairs_once_in_a_fresh_candidate_worktree():
    test_attempts = 0
    worktrees: list[str] = []

    async def run(command, _cwd=None):
        nonlocal test_attempts
        text = " ".join(command) if isinstance(command, list) else command
        if text == "git write-tree":
            return 0, "a" * 40 + "\n"
        if text == "git rev-parse HEAD^{tree}":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "src/demo.py\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text == "python -m pytest -q":
            test_attempts += 1
            return (1, "FAILED tests/test_demo.py::test_it") if test_attempts == 1 else (0, "1 passed")
        if text.startswith("git status --porcelain"):
            return 0, "M src/demo.py"
        if text.startswith("git diff --cached"):
            return 0, "+++ b/src/demo.py\n@@ -0,0 +1 @@\n+value = 1\n"
        return 0, "ok"

    async def initial(worktree):
        worktrees.append(worktree)
        return ["src/demo.py"]

    async def repair(failure: CandidateFailure):
        assert failure.attempt == 1
        assert "FAILED" in failure.test_log

        async def replacement(worktree):
            worktrees.append(worktree)
            return ["src/demo.py"]

        return replacement

    result = asyncio.run(SelfModifier(repo_root="/tmp/hive", run=run).propose(
        "candidate", "", initial, dry_run=True, repair_fn=repair, max_repair_attempts=1,
    ))
    assert result["ok"] is True and result["repair_attempts"] == 1
    assert len(worktrees) == 3 and worktrees[0] != worktrees[1]
    assert worktrees.count(worktrees[0]) == 1
    assert worktrees.count(worktrees[1]) == 2


def test_self_modifier_stops_repair_loop_when_failure_repeats():
    repairs: list[CandidateFailure] = []

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        if text == "git write-tree":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "src/demo.py\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text == "python -m pytest -q":
            return 1, "FAILED tests/test_demo.py::test_it"
        return 0, "ok"

    async def apply(_worktree):
        return ["src/demo.py"]

    async def repair(failure):
        repairs.append(failure)
        return apply

    result = asyncio.run(SelfModifier(repo_root="/tmp/hive", run=run).propose(
        "candidate", "", apply, dry_run=True, repair_fn=repair, max_repair_attempts=3,
    ))
    assert result["stage"] == "repair_no_progress"
    assert len(repairs) == 1


def test_candidate_test_failure_redacts_secret_before_returning_result():
    token = "ghp_" + "a" * 36

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        if text == "git write-tree":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "src/demo.py\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text == "python -m pytest -q":
            return 1, "FAILED with token=" + token
        return 0, "ok"

    async def apply(_worktree):
        return ["src/demo.py"]

    result = asyncio.run(SelfModifier(repo_root="/tmp/hive", run=run).propose("candidate", "", apply))
    assert result["stage"] == "test"
    assert token not in result["log"]


def test_self_improvement_wires_bounded_repair_only_for_auto_edits():
    captured = {}

    class Modifier:
        async def propose(self, *_args, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "stage": "dry_run"}

    async def apply(_worktree):
        return ["tests/test_demo.py"]

    async def repair(_failure):
        return apply

    edit = Edit(op=EditOp.ADD_TEST, summary="add regression", apply=apply,
                target_files=["tests/test_demo.py"])
    outcome = asyncio.run(SelfImprovement(
        Modifier(), repair_factory=lambda _edit: repair, max_repair_attempts=1,
    ).run([edit], dry_run=True))[0]
    assert outcome.status == "applied"
    assert captured["repair_fn"] is repair
    assert captured["max_repair_attempts"] == 1


def test_repair_attempts_are_persisted_in_selfmod_history(tmp_path):
    from hive.observability.persistence import ObservabilityLedger

    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        ledger.record_selfmod({"run_id": "r1", "title": "repair", "repair_attempts": 2})
        assert ledger.selfmod_history()[0]["repair_attempts"] == 2
    finally:
        ledger.close()


def test_real_git_candidate_blocks_secret_before_commit(tmp_path):
    """Exercise the actual worktree + staging boundary in an isolated Git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Hive Test"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

    async def apply(worktree):
        (Path(worktree) / "candidate.py").write_text("token = 'ghp_" + "a" * 36 + "'\n", encoding="utf-8")
        return ["candidate.py"]

    command = f'"{sys.executable}" -c "pass"'
    result = asyncio.run(SelfModifier(repo_root=str(repo), test_cmd=command).propose(
        "candidate", "", apply,
    ))
    assert result["stage"] == "secret_scan"
    assert not list((repo / ".worktrees").glob("hive-auto-*"))
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout
    assert "candidate" not in log


def test_terminal_error_drains_stream_before_runtime_close(capsys):
    from hive.surfaces.cli import _terminal_turn

    class _Hive:
        cleaned = False

        async def stream_ask_iterations(self, *_args, **_kwargs):
            try:
                yield {"type": "error", "class": "NoCredentialsError"}
            finally:
                self.cleaned = True

    hive = _Hive()
    assert asyncio.run(_terminal_turn(hive, "hello", session_id="m4")) == 1
    assert hive.cleaned is True
    assert "No executor API key" in capsys.readouterr().out
