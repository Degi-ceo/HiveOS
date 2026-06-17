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


def test_selfmod_emits_start_and_end_events():
    """SELFMOD_START and SELFMOD_END must be published on the injected EventBus."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), bus=bus)
    asyncio.run(mod.propose("t", "d", _apply_ok))
    types = [e.event_type for e in bus.history()]
    assert EventType.SELFMOD_START in types
    assert EventType.SELFMOD_END in types


def test_selfmod_end_event_carries_outcome():
    """SELFMOD_END event data must include ok, stage."""
    from hive.core.events import EventBus, EventType
    ends = []
    bus = EventBus()
    bus.subscribe(EventType.SELFMOD_END, lambda e: ends.append(e.data))
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), bus=bus)
    asyncio.run(mod.propose("t", "d", _apply_ok))
    assert ends and "ok" in ends[0] and "stage" in ends[0]


def test_github_pr_opener_noops_without_token():
    from hive.core.self_mod import github_pr_opener
    opener = github_pr_opener("", "", "")   # no creds -> no network, returns None
    assert asyncio.run(opener("branch", "t", "b")) is None


def test_empty_apply_fn_returns_no_changes():
    """apply_fn that returns [] (no file changes) should yield stage=no_changes."""
    async def _apply_empty(_wt):
        return []

    def _runner_empty_status():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git status --porcelain"):
                return 0, ""  # empty status = nothing to commit
            if cmd_str.startswith("git push"):
                return 0, "pushed"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_empty_status())
    out = asyncio.run(mod.propose("t", "d", _apply_empty))
    assert out["ok"] is False and out["stage"] == "no_changes"


def test_title_newlines_sanitized():
    """Newlines in the commit title must be stripped before git commit."""
    committed_titles = []

    async def _apply_ok(_wt):
        return ["src/file.py"]

    def _recording_runner():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "abc\n"
            if "commit" in cmd_str and isinstance(cmd, list):
                # The title is the last list element after -m.
                m_idx = cmd.index("-m") if "-m" in cmd else -1
                if m_idx >= 0 and m_idx + 1 < len(cmd):
                    committed_titles.append(cmd[m_idx + 1])
            if "status --porcelain" in cmd_str:
                return 0, "M src/file.py"
            if cmd_str.startswith("git push"):
                return 0, "ok"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_recording_runner())
    asyncio.run(mod.propose("title\nwith\nnewlines", "desc", _apply_ok))
    if committed_titles:
        assert "\n" not in committed_titles[0]
        assert "\r" not in committed_titles[0]


# --- proposal history ---------------------------------------------------------

def test_selfmod_history_recorded_on_success():
    """history() returns the most recent successful proposal."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.last_result is None
    asyncio.run(mod.propose("add feature", "desc", _apply_ok, dry_run=True))
    h = mod.history()
    assert len(h) == 1
    assert h[0]["title"] == "add feature"
    assert h[0]["ok"] is True
    assert h[0]["dry_run"] is True
    assert "ts" in h[0]


def test_selfmod_history_recorded_on_failure():
    """Failed proposals (protected path) also appear in history."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("bad edit", "desc", _apply_protected, dry_run=True))
    h = mod.history()
    assert h and h[0]["ok"] is False


def test_selfmod_last_result_property():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("first", "desc", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("second", "desc", _apply_ok, dry_run=True))
    assert mod.last_result is not None
    assert mod.last_result["title"] == "second"


def test_selfmod_history_newest_first():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "desc", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "desc", _apply_ok, dry_run=True))
    h = mod.history()
    assert h[0]["title"] == "b" and h[1]["title"] == "a"


def test_selfmod_history_capped(monkeypatch):
    """history() never returns more than _MAX_HISTORY entries internally."""
    import hive.core.self_mod as sm
    monkeypatch.setattr(sm, "_MAX_HISTORY", 3)
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for i in range(5):
        asyncio.run(mod.propose(f"edit-{i}", "d", _apply_ok, dry_run=True))
    # Internal list is trimmed to 3; history(limit=10) returns at most 3
    assert len(mod.history(limit=10)) == 3


def test_selfmod_recent_branches_empty_when_no_proposals():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.recent_branches() == []


def test_selfmod_recent_branches_returns_successful_ones():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok1", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("ok2", "d", _apply_ok, dry_run=True))
    branches = mod.recent_branches(n=5)
    assert len(branches) == 2
    assert all(b.startswith("hive/auto-") for b in branches)


def test_selfmod_recent_branches_excludes_failures():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("fail", "d", _apply_protected, dry_run=True))
    branches = mod.recent_branches(n=10)
    assert len(branches) == 1   # only the successful one


def test_selfmod_recent_branches_capped_by_n():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for i in range(5):
        asyncio.run(mod.propose(f"edit-{i}", "d", _apply_ok, dry_run=True))
    assert len(mod.recent_branches(n=3)) == 3


def test_selfmod_clear_history_empty():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.clear_history() == 0


def test_selfmod_clear_history_returns_count():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    assert mod.clear_history() == 2
    assert mod.history() == []
    assert mod.last_result is None


def test_selfmod_proposal_count_zero_initially():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.proposal_count() == 0


def test_selfmod_proposal_count_increments():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    assert mod.proposal_count() == 2


def test_selfmod_proposal_count_resets_after_clear():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    mod.clear_history()
    assert mod.proposal_count() == 0
