"""self_mod flow tests (M2 #si-3 + coverage gap T5) — fake git runner, no network."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier


def _runner(script=None, *, push_rc=0):
    """Fake git runner. `script` maps a command prefix -> (rc, out)."""
    calls = []

    async def run(cmd, cwd=None):
        # cmd may be a list (exec-safe) or a str (shell); normalise to str for matching.
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        calls.append(cmd_str)
        if cmd_str.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if cmd_str.startswith("git push"):
            return push_rc, "push output"
        return 0, "ok"

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def _apply_ok(_wt):
    return ["src/hive/llm/pricing.py"]


async def _apply_protected(_wt):
    return ["Core/approval_gate.py"]


def test_dry_run_skips_push_and_pr():
    run = _runner()
    mod = SelfModifier(repo_root="/tmp/x", run=run)
    out = asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    assert out["ok"] and out["stage"] == "dry_run"
    assert not any(c.startswith("git push") for c in run.calls)


def test_protected_change_refused():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("t", "d", _apply_protected))
    assert not out["ok"] and out["stage"] == "protected"


def test_push_failure_reports_not_ok():
    # Latent bug fix: a failed push must not be reported as success.
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(push_rc=1))
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] is False and out["stage"] == "push"


def test_pr_opener_invoked_on_success():
    opened = {}

    async def fake_opener(branch, title, body):
        opened["branch"] = branch
        opened["title"] = title
        return f"https://github.com/x/y/pull/1"

    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), open_pr=fake_opener)
    out = asyncio.run(mod.propose("my title", "my body", _apply_ok))
    assert out["ok"] and out["stage"] == "pushed"
    assert out["pr_url"] == "https://github.com/x/y/pull/1"
    assert opened["title"] == "my title" and opened["branch"].startswith("hive/auto-")


def test_pr_opener_failure_keeps_branch_but_flags_note():
    async def failing_opener(branch, title, body):
        return None  # opening failed

    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), open_pr=failing_opener)
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] and out["pr_url"] is None and "failed" in out["note"]


def test_no_opener_keeps_push_only_note():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] and "pr_url" not in out and "never merges" in out["note"]


def test_github_pr_opener_noops_without_token():
    from hive.core.self_mod import github_pr_opener
    opener = github_pr_opener("", "", "")   # no creds -> no network, returns None
    assert asyncio.run(opener("branch", "t", "b")) is None
