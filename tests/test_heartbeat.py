"""Heartbeat — dispatch error paths, planner enqueue, self-improve threshold, run/stop.

Targets the missed lines reported in COVERAGE_REPORT_2026-06.md for
hive.autonomy.heartbeat (was 70% — missing dispatch error branches,
planner enqueue loop, self-improve threshold, proactive exception path,
run() loop and stop()).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hive.autonomy.heartbeat import Heartbeat
from hive.tools.executor import DispatchStatus, ToolDispatch


def _mock_hive(*, proactive_interval: int = 0,
               failure_threshold: int = 3,
               failure_cooldown_sec: float = 0.0,
               due: list | None = None) -> MagicMock:
    """Build a fully-wired MagicMock hive matching heartbeat's _tick_inner contract."""
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.autonomy_enabled = True
    hive.config.autonomous_selfmod_enabled = True
    hive.config.selfmod_failure_threshold = failure_threshold
    hive.config.selfmod_proactive_interval = proactive_interval
    hive.config.selfmod_failure_cooldown_sec = failure_cooldown_sec
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = list(due or [])
    hive.task_board.recent_failures.return_value = []
    hive.task_board.claim.return_value = True
    hive.planner = MagicMock()
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = "ctx"
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_diagnose = AsyncMock(return_value={"improvement_outcomes": []})
    hive.self_improve_from_symptom = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    hive.self_modifier.sweep_orphaned_worktrees = AsyncMock(
        return_value={"removed": [], "errors": []})
    return hive


# --- enqueue() manual path (line 42) -----------------------------------------

def test_heartbeat_enqueue_returns_task_id():
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.task_board.enqueue.return_value = 42
    hb = Heartbeat(hive)
    assert hb.enqueue({"tool": "shell"}) == 42
    hive.task_board.enqueue.assert_called_once_with(
        "tool", {"tool": "shell"}, source="manual")


# --- planner enqueue loop (line 64) ------------------------------------------

def test_heartbeat_planner_enqueues_each_planned_task():
    hive = _mock_hive()
    hive.planner.plan = AsyncMock(return_value=[{"tool": "a"}, {"tool": "b"}])
    # After planner enqueues, board.due is called again to fetch due tasks
    hive.task_board.due.side_effect = [[], []]   # first call: empty → plan; second: empty
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["planned"] == 2
    # Two planner enqueues on the board (source="planner")
    planner_enqueues = [c for c in hive.task_board.enqueue.call_args_list
                        if c.kwargs.get("source") == "planner"
                        or (len(c.args) >= 3 and c.args[2] == "planner")]
    assert len(planner_enqueues) == 2

def test_heartbeat_disabled_does_not_schedule_or_dispatch():
    hive = _mock_hive()
    hive.config.autonomy_enabled = False
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["disabled"] is True
    hive.cron.due_and_enqueue.assert_not_called()
    hive.commitments.due_and_enqueue.assert_not_called()
    hive.task_board.due.assert_not_called()
    hive.tool_executor.execute.assert_not_called()


# --- try/except swallowing in tick (lines 72-74, 78-79, 82-83) --------------

def test_heartbeat_consolidate_failure_does_not_abort_tick():
    hive = _mock_hive()
    hive.consolidate = AsyncMock(side_effect=RuntimeError("disk full"))
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["consolidated"] == 0      # falls back to 0
    assert summary["dispatched"] == 0        # tick still returned


def test_heartbeat_curate_umbrellas_failure_does_not_abort_tick():
    hive = _mock_hive()
    hive.curate_umbrellas = AsyncMock(side_effect=ValueError("bad json"))
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    # Tick completes; curate=0 (empty transitions)
    assert summary["curated"] == 0


def test_heartbeat_budget_refresh_failure_does_not_abort_tick():
    hive = _mock_hive()
    hive.budgeter.refresh = AsyncMock(side_effect=ConnectionError("api down"))
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    # Tick completes; self_improved/proactive both 0
    assert summary["self_improved"] == 0
    assert summary["proactive_diagnosed"] == 0


# --- self-improve from symptom threshold (lines 92-97) -----------------------

def test_heartbeat_self_improve_fires_when_recent_failures_exceed_threshold():
    hive = _mock_hive(failure_threshold=3)
    # 3 failed task records (must have .last_error attribute)
    failed = []
    for err in ["timeout", "auth", "missing file"]:
        rec = MagicMock()
        rec.last_error = err
        failed.append(rec)
    hive.task_board.recent_failures.return_value = failed
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["self_improved"] == 2    # self_improve returned 2 outcomes
    hive.self_improve_from_symptom.assert_awaited_once()
    symptom = hive.self_improve_from_symptom.await_args.args[0]
    assert "timeout" in symptom and "auth" in symptom and "missing file" in symptom


def test_heartbeat_self_improve_is_disabled_without_selfmod_gate():
    hive = _mock_hive(failure_threshold=1)
    hive.config.autonomous_selfmod_enabled = False
    hive.task_board.recent_failures.return_value = [MagicMock(last_error="timeout")]
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["self_improved"] == 0
    hive.self_improve_from_symptom.assert_not_awaited()


def test_heartbeat_self_improve_skips_below_threshold():
    hive = _mock_hive(failure_threshold=3)
    failed = [MagicMock(last_error="e1"), MagicMock(last_error="e2")]
    hive.task_board.recent_failures.return_value = failed
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    assert summary["self_improved"] == 0
    hive.self_improve_from_symptom.assert_not_called()


def test_heartbeat_self_improve_exception_is_swallowed():
    hive = _mock_hive(failure_threshold=2)
    failed = [MagicMock(last_error="a"), MagicMock(last_error="b")]
    hive.task_board.recent_failures.return_value = failed
    hive.self_improve_from_symptom = AsyncMock(
        side_effect=RuntimeError("self-mod gate denied"))
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    # Exception caught — tick still returns 0
    assert summary["self_improved"] == 0


# --- proactive self-diagnose exception (lines 113-114) ----------------------

def test_heartbeat_proactive_self_diagnose_exception_is_swallowed():
    hive = _mock_hive(proactive_interval=1)        # fire every tick
    hive.self_diagnose = AsyncMock(side_effect=RuntimeError("model timeout"))
    summary = asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    # Exception caught — tick completes; outcome count is 0 (we set it before
    # the exception path; the test asserts that the tick does not raise).
    assert "proactive_diagnosed" in summary


# --- _dispatch() error paths (lines 132, 136-137, 144-147) -------------------

def _make_record(task_id: int = 1, tool: str | None = "x", args: dict | None = None):
    rec = MagicMock()
    rec.id = task_id
    rec.payload = {"tool": tool, "args": args or {}}
    return rec


def test_dispatch_claim_failure_skips_task():
    """run_one returns False when board.claim returns False (race / restart)."""
    hive = _mock_hive()
    hive.task_board.claim.return_value = False
    rec = _make_record()
    dispatched = asyncio.run(Heartbeat(hive)._dispatch([rec]))
    assert dispatched == 0
    hive.tool_executor.execute.assert_not_called()


def test_dispatch_no_tool_marks_task_complete_and_skips():
    """run_one returns False and calls board.complete when payload lacks 'tool'."""
    hive = _mock_hive()
    rec = _make_record(tool=None)
    dispatched = asyncio.run(Heartbeat(hive)._dispatch([rec]))
    assert dispatched == 0
    hive.task_board.complete.assert_called_once_with(rec.id)
    hive.tool_executor.execute.assert_not_called()


def test_dispatch_tool_exception_marks_task_failed():
    """run_one catches tool executor exceptions and records the failure on the board."""
    hive = _mock_hive()
    hive.tool_executor.execute = AsyncMock(side_effect=RuntimeError("boom"))
    rec = _make_record(tool="read_file", args={"path": "/missing"})
    dispatched = asyncio.run(Heartbeat(hive)._dispatch([rec]))
    assert dispatched == 0
    hive.task_board.complete.assert_not_called()
    hive.task_board.fail.assert_called_once()
    args, _ = hive.task_board.fail.call_args
    assert args[0] == rec.id and "boom" in args[1]


def test_dispatch_structured_error_marks_task_failed_not_complete():
    """ToolExecutor encodes ordinary failures in ToolDispatch, not exceptions."""
    hive = _mock_hive()
    hive.tool_executor.execute = AsyncMock(return_value=ToolDispatch(
        DispatchStatus.ERROR, error="unknown tool: vanished"))
    rec = _make_record(tool="vanished")

    dispatched = asyncio.run(Heartbeat(hive)._dispatch([rec]))

    assert dispatched == 0
    hive.task_board.complete.assert_not_called()
    hive.task_board.fail.assert_called_once_with(rec.id, "unknown tool: vanished")


def test_dispatch_pending_approval_waits_without_completing_or_failing():
    """A gated task remains durable but is not a tool failure until decided."""
    hive = _mock_hive()
    hive.tool_executor.execute = AsyncMock(return_value=ToolDispatch(
        DispatchStatus.PENDING, approval_id="approval-42"))
    rec = _make_record(tool="deploy")

    dispatched = asyncio.run(Heartbeat(hive)._dispatch([rec]))

    assert dispatched == 0
    hive.task_board.complete.assert_not_called()
    hive.task_board.fail.assert_not_called()
    hive.task_board.await_approval.assert_called_once_with(rec.id, "approval-42")


# --- run() loop + stop() (lines 157-169, 172) --------------------------------

def test_run_loop_ticks_periodically_and_stop_breaks():
    """run() drives tick() in a loop; stop() flips the running flag."""
    hive = _mock_hive()
    # Make tick() fast by replacing it with an AsyncMock
    tick_results = [{"cron": 0, "commitments": 0, "planned": 0,
                     "dispatched": 0, "consolidated": 0, "curated": 0,
                     "self_improved": 0, "proactive_diagnosed": 0}]
    tick_mock = AsyncMock(side_effect=tick_results)
    hb = Heartbeat(hive)
    hb.tick = tick_mock   # patch the instance method

    async def driver():
        task = asyncio.create_task(hb.run(interval=0.01))
        await asyncio.sleep(0.05)   # let it tick ~3-5 times
        assert tick_mock.call_count >= 1
        hb.stop()
        await asyncio.wait_for(task, timeout=1.0)
    asyncio.run(driver())
    assert hive.task_board.requeue_running.called   # recovered on startup
    assert hive.self_modifier.sweep_orphaned_worktrees.called   # Batch I: worktrees too
    assert hb._running is False


def test_run_loop_survives_worktree_sweep_failure():
    """A raising sweep_orphaned_worktrees() must not prevent the heartbeat loop
    from starting (fail-open startup recovery, matching requeue_running's
    treatment elsewhere)."""
    hive = _mock_hive()
    hive.self_modifier.sweep_orphaned_worktrees = AsyncMock(
        side_effect=RuntimeError("git unavailable"))
    tick_mock = AsyncMock(return_value={"cron": 0, "commitments": 0, "planned": 0,
                                        "dispatched": 0, "consolidated": 0, "curated": 0,
                                        "self_improved": 0, "proactive_diagnosed": 0})
    hb = Heartbeat(hive)
    hb.tick = tick_mock

    async def driver():
        task = asyncio.create_task(hb.run(interval=0.01))
        await asyncio.sleep(0.05)
        assert tick_mock.call_count >= 1
        hb.stop()
        await asyncio.wait_for(task, timeout=1.0)
    asyncio.run(driver())


def test_stop_sets_running_false():
    hive = _mock_hive()
    hb = Heartbeat(hive)
    hb._running = True
    hb.stop()
    assert hb._running is False
