"""Focused durable-state tests for the M10 operator-goal ledger."""
from __future__ import annotations

import threading
import pytest

from hive.autonomy.goals import (
    BLOCKED,
    CANCELLED,
    COMPLETED,
    EVALUATING,
    EXECUTING,
    OPEN,
    PLANNING,
    REPLANNING,
    GoalLedger,
)


@pytest.fixture(autouse=True)
def _machine_identity(monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m10-test-machine")


def test_goal_is_redacted_durable_and_queryable_after_restart(tmp_path):
    db = tmp_path / "state.sqlite"
    ledger = GoalLedger(db)
    goal = ledger.create("Inspect token=super-secret-token\nand report a safe result")

    assert goal.status == OPEN
    assert "super-secret-token" not in goal.summary
    assert goal.summary != "Inspect token=super-secret-token and report a safe result"

    reopened = GoalLedger(db)
    restored = reopened.get(goal.goal_id)

    assert restored is not None
    assert restored.summary == goal.summary
    assert reopened.list(statuses={OPEN}) == [restored]


def test_planning_claim_is_atomic_across_ledger_connections(tmp_path):
    db = tmp_path / "state.sqlite"
    creator = GoalLedger(db)
    goal = creator.create("Prepare a bounded implementation plan")
    first = GoalLedger(db)
    second = GoalLedger(db)
    barrier = threading.Barrier(2)
    claims = []

    def claim(ledger):
        barrier.wait()
        claims.append(ledger.claim_planning(goal.goal_id))

    workers = [threading.Thread(target=claim, args=(ledger,)) for ledger in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert claimed[0].status == PLANNING
    assert claimed[0].plan_generation == 1
    assert creator.get(goal.goal_id).status == PLANNING


def test_foreign_host_cannot_claim_owner_goal(tmp_path):
    db = tmp_path / "state.sqlite"
    owner = GoalLedger(db, hostname="owner-host", machine_identity="owner-machine")
    foreign = GoalLedger(db, hostname="foreign-host", machine_identity="foreign-machine")
    goal = owner.create("owner-only intent")

    assert foreign.claim_planning(goal.goal_id) is None
    assert owner.get(goal.goal_id).status == OPEN
    assert owner.claim_planning(goal.goal_id) is not None


def test_foreign_active_goal_does_not_make_owner_ledger_busy(tmp_path):
    db = tmp_path / "state.sqlite"
    foreign = GoalLedger(db, hostname="foreign-host", machine_identity="foreign-machine")
    local = GoalLedger(db, hostname="local-host", machine_identity="local-machine")
    goal = foreign.create("foreign work")
    assert foreign.claim_planning(goal.goal_id) is not None

    assert all(record.owner_host != local._owner_host
               for record in local.list(statuses={"planning", "executing", "evaluating"}))


def test_goal_creation_fails_closed_without_machine_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_STATE_HOST_ID")
    ledger = GoalLedger(tmp_path / "state.sqlite", machine_identity="")
    with pytest.raises(RuntimeError, match="HIVE_STATE_HOST_ID"):
        ledger.create("must not become cross-host work")


def test_task_references_and_two_bounded_replans(tmp_path):
    ledger = GoalLedger(tmp_path / "state.sqlite")
    goal = ledger.create("Repair token=another-secret safely")

    first = ledger.claim_planning(goal.goal_id)
    assert first is not None
    executing = ledger.begin_execution(goal.goal_id, [8, 8, 13], expected_generation=1)
    assert executing is not None
    assert executing.status == EXECUTING
    assert executing.task_ids == (8, 13)
    assert ledger.begin_evaluation(goal.goal_id).status == EVALUATING

    first_replan = ledger.request_replan(goal.goal_id, "tool error token=repair-secret")
    assert first_replan is not None
    assert first_replan.status == REPLANNING
    assert first_replan.replan_count == 1
    assert "repair-secret" not in first_replan.last_reason

    second_plan = ledger.claim_planning(goal.goal_id)
    assert second_plan is not None
    assert second_plan.plan_generation == 2
    assert ledger.begin_execution(goal.goal_id, [21], expected_generation=2).status == EXECUTING
    assert ledger.begin_evaluation(goal.goal_id).status == EVALUATING
    assert ledger.request_replan(goal.goal_id, "first retry failed").status == REPLANNING

    third_plan = ledger.claim_planning(goal.goal_id)
    assert third_plan is not None
    assert third_plan.plan_generation == 3
    assert ledger.begin_execution(goal.goal_id, [34], expected_generation=3).status == EXECUTING
    assert ledger.begin_evaluation(goal.goal_id).status == EVALUATING

    exhausted = ledger.request_replan(goal.goal_id, "second retry failed")
    assert exhausted is not None
    assert exhausted.status == BLOCKED
    assert exhausted.replan_count == 2
    assert ledger.task_references(goal.goal_id, generation=1) == (8, 13)
    assert ledger.task_references(goal.goal_id, generation=3) == (34,)


def test_completion_and_safe_pre_execution_cancellation(tmp_path):
    ledger = GoalLedger(tmp_path / "state.sqlite")
    completed = ledger.create("Check service health")
    assert ledger.claim_planning(completed.goal_id) is not None
    assert ledger.begin_execution(completed.goal_id, [5]).status == EXECUTING
    assert ledger.begin_evaluation(completed.goal_id).status == EVALUATING
    assert ledger.complete(completed.goal_id).status == COMPLETED

    cancelled = ledger.create("Do not start this task")
    assert ledger.cancel(cancelled.goal_id).status == CANCELLED
    assert ledger.claim_planning(cancelled.goal_id) is None
    resumed = ledger.resume(cancelled.goal_id)
    assert resumed is not None and resumed.status == OPEN
    assert ledger.claim_planning(cancelled.goal_id).status == PLANNING
