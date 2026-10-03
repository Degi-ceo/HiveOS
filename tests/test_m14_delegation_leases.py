"""M14 — queue-safe capability leases for bounded delegation branches."""
from __future__ import annotations

import asyncio

from hive.agents.delegations import DelegationLedger
from hive.agents.worker_protocol import WorkerRequest
from hive.agents.worker_supervisor import LocalWorkerSupervisor, _TrustedTurnState
from hive.core.types import Message, Role
from hive.tools.executor import ToolExecutor


def _claimed_coordinator(tmp_path, now):
    ledger = DelegationLedger(
        tmp_path / "state.sqlite", machine_identity="m14", clock=lambda: now[0],
    )
    root = ledger.create(parent_run_id="root", child_run_id="coordinator", role="coordinator")
    assert root.capability_state == "issued" and root.capability_deadline_ts == 0
    assert ledger.claim(root.id) == 1
    root = ledger.get(root.id)
    assert root is not None
    return ledger, root


def test_queued_child_lease_starts_only_when_it_claims(tmp_path):
    now = [100.0]
    ledger, root = _claimed_coordinator(tmp_path, now)
    assert root.capability_deadline_ts == 210.0

    first = ledger.create(parent_run_id="coordinator", child_run_id="first", role="researcher",
                          parent_delegation_id=root.id)
    second = ledger.create(parent_run_id="coordinator", child_run_id="second", role="reviewer",
                           parent_delegation_id=root.id)
    assert ledger.claim(first.id) == 1
    assert ledger.get(second.id).capability_state == "issued"

    now[0] = 125.0
    assert ledger.finish(first.id, attempt=1, success=True)
    assert ledger.claim(second.id) == 1
    second = ledger.get(second.id)
    assert second is not None
    assert second.capability_state == "active"
    assert second.capability_deadline_ts == 155.0
    assert ledger.authorize_attempt(second.id, capability_id=second.capability_id,
                                    role="reviewer", attempt=1)


def test_claim_uses_time_after_acquiring_sqlite_writer_lock(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14", clock=lambda: 100.0)
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    # Simulate time advancing while BEGIN IMMEDIATE waits on another writer.
    ledger._clock = lambda: 104.0 if ledger._db.in_transaction else 100.0
    assert ledger.claim(item.id) == 1
    claimed = ledger.get(item.id)
    assert claimed is not None
    assert claimed.capability_deadline_ts == 104.0 + item.max_worker_seconds


def test_claim_rechecks_parent_expiry_after_acquiring_writer_lock(tmp_path):
    now = [100.0]
    ledger, root = _claimed_coordinator(tmp_path, now)
    child = ledger.create(parent_run_id="coordinator", child_run_id="child", role="researcher",
                          parent_delegation_id=root.id)
    ledger._clock = lambda: (root.capability_deadline_ts + 1.0 if ledger._db.in_transaction
                             else root.capability_deadline_ts - 1.0)
    assert ledger.claim(child.id) is None
    assert ledger.get(child.id).state == "queued"
    assert ledger.resource_snapshot(child.id)["state"] == "queued"


def test_full_branch_model_leases_never_exceed_its_reserved_turn_budget(tmp_path):
    now = [100.0]
    ledger, root = _claimed_coordinator(tmp_path, now)
    children = [
        ledger.create(parent_run_id="coordinator", child_run_id=f"child-{index}", role="researcher",
                      parent_delegation_id=root.id)
        for index in range(3)
    ]
    limits = [ledger.resource_snapshot(root.id)["max_model_calls"]]
    for child in children:
        assert ledger.claim(child.id) == 1
        limits.append(ledger.resource_snapshot(child.id)["max_model_calls"])
        assert ledger.finish(child.id, attempt=1, success=True)
    assert all(isinstance(limit, int) for limit in limits)
    assert sum(limits) == root.branch_max_turns


def test_terminal_parent_revokes_a_queued_child_grant(tmp_path):
    now = [100.0]
    ledger, root = _claimed_coordinator(tmp_path, now)
    child = ledger.create(parent_run_id="coordinator", child_run_id="child", role="researcher",
                          parent_delegation_id=root.id)
    assert child.capability_state == "issued"
    assert ledger.finish(root.id, attempt=1, success=True)
    child = ledger.get(child.id)
    assert child is not None and child.capability_state == "revoked"
    assert ledger.claim(child.id) is None


def test_resource_lease_counts_aggregate_model_and_tool_units_durably(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    item = ledger.get(item.id)
    assert item is not None

    model_limit = ledger.resource_snapshot(item.id)["max_model_calls"]
    assert isinstance(model_limit, int)
    for _ in range(model_limit):
        assert ledger.consume_model_call(item.id, capability_id=item.capability_id,
                                         role=item.role, attempt=1)
    assert not ledger.consume_model_call(item.id, capability_id=item.capability_id,
                                         role=item.role, attempt=1)
    for _ in range(item.max_worker_tool_calls):
        assert ledger.consume_tool_call(item.id, capability_id=item.capability_id,
                                        role=item.role, attempt=1)
    assert not ledger.consume_tool_call(item.id, capability_id=item.capability_id,
                                        role=item.role, attempt=1)

    reopened = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14")
    snapshot = reopened.resource_snapshot(item.id)
    assert snapshot == {
        "state": "active", "model_calls_used": model_limit,
        "max_model_calls": model_limit, "tool_calls_used": item.max_worker_tool_calls,
        "max_tool_calls": item.max_worker_tool_calls,
    }
    reopened.close()


def test_terminal_delegation_closes_its_resource_lease(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    item = ledger.get(item.id)
    assert item is not None
    assert ledger.consume_model_call(item.id, capability_id=item.capability_id,
                                     role=item.role, attempt=1)
    assert ledger.finish(item.id, attempt=1, success=True)
    snapshot = ledger.resource_snapshot(item.id)
    assert snapshot["state"] == "completed"
    assert snapshot["model_calls_used"] == 1
    assert not ledger.consume_model_call(item.id, capability_id=item.capability_id,
                                         role=item.role, attempt=1)


def test_proven_interrupted_local_delegation_closes_its_lease(tmp_path):
    db_path = tmp_path / "state.sqlite"
    owner = DelegationLedger(
        db_path, machine_identity="m14", process_id=11, process_is_alive=lambda _pid: False,
    )
    item = owner.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert owner.claim(item.id) == 1
    owner.close()

    restarted = DelegationLedger(
        db_path, machine_identity="m14", process_id=12, process_is_alive=lambda _pid: False,
    )
    assert restarted.recover_interrupted() == 1
    assert restarted.resource_snapshot(item.id)["state"] == "failed"
    restarted.close()


def test_supervisor_refuses_exhausted_model_lease_before_router_call(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    item = ledger.get(item.id)
    assert item is not None
    model_limit = ledger.resource_snapshot(item.id)["max_model_calls"]
    assert isinstance(model_limit, int)
    for _ in range(model_limit):
        assert ledger.consume_model_call(item.id, capability_id=item.capability_id,
                                         role=item.role, attempt=1)

    class Router:
        calls = 0

        async def complete(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("exhausted lease must block before the router")

    router = Router()
    supervisor = LocalWorkerSupervisor(router, {}, delegation_ledger=ledger)
    request = WorkerRequest(role=item.role, task="x", run_id="run", delegation_id=item.id,
                            capability_id=item.capability_id, max_iterations=item.max_worker_turns)
    state = _TrustedTurnState(messages=[Message(role=Role.USER, content="x")], pending={})
    reply = asyncio.run(supervisor._handle(
        {"type": "model", "tools": []}, request, ToolExecutor({}), state, frozenset(), 1,
    ))
    assert reply == {"error": "WorkerProtocolError"}
    assert router.calls == 0


def test_lease_allows_the_existing_final_loop_guard_pivot(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m14")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    item = ledger.get(item.id)
    assert item is not None

    class Router:
        calls = 0

        async def complete(self, *_args, **_kwargs):
            self.calls += 1
            from hive.llm.adapters.base import CompletionResult
            return CompletionResult(text="safe pivot", model="test")

    router = Router()
    supervisor = LocalWorkerSupervisor(router, {}, delegation_ledger=ledger)
    request = WorkerRequest(role=item.role, task="x", run_id="run", delegation_id=item.id,
                            capability_id=item.capability_id,
                            max_iterations=max(1, item.max_worker_turns - 1))
    state = _TrustedTurnState(
        messages=[Message(role=Role.USER, content="x")], pending={}, model_calls=request.max_iterations,
    )
    reply = asyncio.run(supervisor._handle(
        {"type": "model", "tools": []}, request, ToolExecutor({}), state, frozenset(), 1,
    ))
    assert reply["result"]["text"] == "safe pivot"
    assert router.calls == 1
