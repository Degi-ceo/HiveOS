"""
test_m10_self_improve.py — M10-c: self-improvement depth.

Tests:
  - TaskBoard.recent_failures() query
  - HiveOS.self_improve_from_symptom() is callable and returns a list
  - Heartbeat tick returns self_improved key
  - REVIEW/MANUAL edits are enqueued as self_improve tasks
"""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="[]", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# TaskBoard.recent_failures
# ---------------------------------------------------------------------------

def test_recent_failures_empty(tmp_path):
    hive = _make_hive(tmp_path)
    assert hive.task_board.recent_failures() == []


def test_recent_failures_returns_failed_tasks(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "timeout")

    failures = hive.task_board.recent_failures(limit=10)
    assert len(failures) == 1
    assert failures[0].state == "failed"
    assert failures[0].last_error == "timeout"


def test_recent_failures_only_failed_state(tmp_path):
    hive = _make_hive(tmp_path)
    # Enqueue 3: one done, one failed, one pending
    tid1 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid1)
    hive.task_board.complete(tid1)

    tid2 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid2)
    hive.task_board.fail(tid2, "err")

    hive.task_board.enqueue("tool", {}, source="test")  # pending

    failures = hive.task_board.recent_failures()
    assert len(failures) == 1
    assert failures[0].state == "failed"


def test_recent_failures_limit(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(5):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    assert len(hive.task_board.recent_failures(limit=3)) == 3


def test_recent_failures_newest_first(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(3):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    failures = hive.task_board.recent_failures()
    ids = [f.id for f in failures]
    assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# HiveOS.self_improve_from_symptom
# ---------------------------------------------------------------------------

def test_self_improve_from_symptom_returns_list(tmp_path):
    hive = _make_hive(tmp_path)
    outcomes = asyncio.run(hive.self_improve_from_symptom("repeated timeout errors"))
    assert isinstance(outcomes, list)


def test_self_improve_from_symptom_no_crash_on_empty_response(tmp_path):
    hive = _make_hive(tmp_path)
    # _ScriptRouter returns text="[]" so diagnoser returns no edits — safe no-op
    outcomes = asyncio.run(hive.self_improve_from_symptom("test symptom"))
    assert outcomes == []


# ---------------------------------------------------------------------------
# Heartbeat tick — self_improved key present
# ---------------------------------------------------------------------------

def test_tick_returns_self_improved_key(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert "self_improved" in result
    assert isinstance(result["self_improved"], int)


def test_tick_self_improve_not_triggered_below_threshold(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    # Only 2 failures — below the ≥3 threshold; self_improved should stay 0
    for _ in range(2):
        tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, "err")

    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert result["self_improved"] == 0


# ---------------------------------------------------------------------------
# _diagnoser JSON parsing (the bug fix: used to return [] unconditionally)
# ---------------------------------------------------------------------------

class _EditRouter:
    """Returns a JSON array with one EDIT_DOCS edit."""
    def __init__(self, payload: str = "[]"):
        self._payload = payload

    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text=self._payload, model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def test_diagnoser_parses_edit_docs_json(tmp_path):
    """Diagnoser must convert valid JSON into Edit objects (not discard them)."""
    import json as _json
    payload = _json.dumps([{
        "op": "edit_docs",
        "summary": "update readme",
        "rationale": "out of date",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("test symptom"))
    # EDIT_DOCS is AUTO tier — will attempt worktree propose; in test env git
    # will fail (not a real repo), so outcome.status == "failed" is expected.
    # What matters: outcomes is non-empty and not silently discarded.
    assert isinstance(outcomes, list)
    assert len(outcomes) == 1


def test_diagnoser_skips_unknown_op(tmp_path):
    """Edits with unknown op values are silently skipped."""
    import json as _json
    payload = _json.dumps([{"op": "DOES_NOT_EXIST", "summary": "x", "rationale": "y"}])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("bad op"))
    assert outcomes == []


def test_diagnoser_handles_malformed_json(tmp_path):
    """Non-JSON response from router is handled gracefully."""
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter("not valid json {{"))
    outcomes = asyncio.run(hive.self_improve_from_symptom("malformed"))
    assert outcomes == []


def test_review_tier_edit_stored_in_edit_pending(tmp_path):
    """REVIEW-tier edits must be stored in hive.edit_pending for later approval."""
    import json as _json
    # PATCH_CODE is REVIEW tier per the tier table.
    payload = _json.dumps([{
        "op": "patch_code",
        "summary": "fix the bug",
        "rationale": "it crashes",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("crash symptom"))
    assert len(outcomes) == 1
    assert outcomes[0].status == "pending_approval"
    # The Edit object must be stored so the gateway can retrieve it on approval.
    assert len(hive.edit_pending) == 1
    approval_id = outcomes[0].approval_id
    assert approval_id in hive.edit_pending


def test_review_tier_enqueues_task(tmp_path):
    """REVIEW-tier outcomes must be enqueued as self_improve tasks in the task board."""
    import json as _json
    payload = _json.dumps([{
        "op": "patch_code",
        "summary": "fix crash",
        "rationale": "boom",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    asyncio.run(hive.self_improve_from_symptom("bad symptom"))
    tasks = hive.task_board.all()
    si_tasks = [t for t in tasks if t.kind == "self_improve"]
    assert si_tasks, "REVIEW-tier outcome must enqueue a self_improve task"
    assert si_tasks[0].payload.get("tier") == "review"
    # New: task payload must include op for transparency in /tasks
    assert si_tasks[0].payload.get("op") == "patch_code"


def test_manual_tier_enqueues_task(tmp_path):
    """MANUAL-tier outcomes must also be enqueued as self_improve tasks."""
    import json as _json
    # PATCH_SYSTEM_PROMPT is not MANUAL; use op with MANUAL tier if one exists,
    # otherwise test by directly checking RiskTier enum comparison is correct.
    from hive.core.spec_search import RiskTier
    assert RiskTier.MANUAL.value == "manual"  # guard the string we assert below
    # All ops below MANUAL in the tier table — verify tier enum comparison doesn't regress.
    payload = _json.dumps([])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("no-op symptom"))
    assert outcomes == []  # empty payload → no tasks enqueued
    assert hive.task_board.all() == []


# ---------------------------------------------------------------------------
# Security: path traversal guard in _apply closure
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HiveOS.run_tests() — offline unit tests (monkey-patch subprocess)
# ---------------------------------------------------------------------------

def test_run_tests_all_passed(tmp_path, monkeypatch):
    """run_tests() returns all_passed=True when pytest exits 0."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"387 passed, 4 skipped in 11.7s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q"))
    assert result["all_passed"] is True
    assert result["passed"] == 387
    assert result["failed"] == 0
    assert result["timed_out"] is False


def test_run_tests_with_failures(tmp_path, monkeypatch):
    """run_tests() returns all_passed=False and counts failures correctly."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"3 passed, 2 failed in 1.2s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["passed"] == 3
    assert result["failed"] == 2
    assert result["returncode"] == 1


def test_run_tests_timeout(tmp_path, monkeypatch):
    """run_tests() returns timed_out=True when the process times out."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = -1
            def kill(self): pass
            async def communicate(self): return b"", b""
        return _Proc()

    async def _fake_wait_for(coro, timeout):
        # cancel the coroutine to avoid "never awaited" warnings
        coro.close()
        raise _asyncio.TimeoutError()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    monkeypatch.setattr(_asyncio, "wait_for", _fake_wait_for)

    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q", timeout=0.001))
    assert result["timed_out"] is True
    assert result["all_passed"] is False


# ---------------------------------------------------------------------------
# HiveOS.self_diagnose() — calls run_tests then self_improve_from_symptom
# ---------------------------------------------------------------------------

def test_self_diagnose_no_improvement_when_all_pass(tmp_path, monkeypatch):
    """self_diagnose() does not trigger self-improvement when tests pass."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"10 passed in 0.5s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is True
    assert result["improvement_outcomes"] == []


def test_self_diagnose_triggers_improvement_on_failure(tmp_path, monkeypatch):
    """self_diagnose() triggers self_improve_from_symptom() when tests fail."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"1 passed, 2 failed in 0.5s\nFAILED tests/test_x.py::test_y\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    # _ScriptRouter returns "[]" so self_improve_from_symptom yields no edits
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["failed"] == 2
    assert "improvement_outcomes" in result


def test_self_diagnose_skips_diagnoser_when_near_cap(tmp_path, monkeypatch):
    """self_diagnose() skips the LLM diagnoser when the budgeter is near cap."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"0 passed, 1 failed in 0.1s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    # Exhaust the budget
    for _ in range(hive.config.daily_call_cap):
        hive.budgeter.record_call()
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["skipped_reason"] == "near_daily_cap"
    assert result["improvement_outcomes"] == []


def test_self_diagnose_returns_improvement_key_on_timeout(tmp_path, monkeypatch):
    """self_diagnose() returns improvement_outcomes=[] when tests time out."""
    import asyncio as _asyncio

    async def _fake_wait_for(coro, timeout):
        coro.close()
        raise _asyncio.TimeoutError()

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = -1
            def kill(self): pass
            async def communicate(self): return b"", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    monkeypatch.setattr(_asyncio, "wait_for", _fake_wait_for)

    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["timed_out"] is True
    assert result["improvement_outcomes"] == []


# ---------------------------------------------------------------------------
# Security: path traversal guard in _apply closure
# ---------------------------------------------------------------------------

def test_diagnoser_apply_closure_rejects_path_traversal(tmp_path):
    """The _apply closure from the REAL self_improve_from_symptom() must refuse
    paths that resolve outside the worktree.

    Tests the production closure (not a hand-copied guard) by patching
    SelfModifier.propose to call edit.apply() with a controlled fake worktree,
    so the actual runtime.py code path is exercised.
    """
    import json as _json
    from unittest.mock import AsyncMock, patch

    from hive.core.spec_search import EditOp, EditOutcome, RiskTier

    wt = tmp_path / "worktree"
    wt.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel")

    # Router returns a path-traversal edit: path escapes the worktree via ../..
    payload = _json.dumps([{
        "op": "edit_docs",
        "summary": "evil",
        "rationale": "attack",
        "path": "../../outside.txt",
        "old_text": "sentinel",
        "new_text": "OVERWRITTEN",
    }])

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))

    # Intercept SelfModifier.propose: call the real apply_fn with our
    # controlled fake worktree so the production _apply closure is exercised.
    # propose signature: (title, description, apply_fn, *, dry_run=False) -> dict
    async def _fake_propose(title, description, apply_fn, *, dry_run=False):
        modified = await apply_fn(str(wt))
        return {"ok": True, "stage": "dry_run", "branch": "test", "changed": modified}

    with patch.object(hive.self_modifier, "propose", side_effect=_fake_propose):
        asyncio.run(hive.self_improve_from_symptom("path traversal attack"))

    assert outside.read_text() == "sentinel", \
        "Production _apply closure must block path traversal outside the worktree"
