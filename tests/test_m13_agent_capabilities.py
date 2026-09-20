"""M13 capability grants are immutable and parent-enforced."""
import asyncio

from hive.agents.delegations import DelegationLedger
from hive.agents.worker_protocol import WorkerRequest
from hive.agents.worker_supervisor import LocalWorkerSupervisor
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult


def test_active_grant_authorizes_only_its_claimed_attempt(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert item.capability_id and item.capability_state == "active"
    assert ledger.claim(item.id) == 1
    assert ledger.authorize_attempt(item.id, capability_id=item.capability_id,
                                    role="researcher", attempt=1)
    assert not ledger.authorize_attempt(item.id, capability_id="forged",
                                        role="researcher", attempt=1)


def test_grant_expiry_fails_closed(tmp_path):
    now = [100.0]
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13", clock=lambda: now[0])
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    now[0] = item.capability_deadline_ts + 1
    assert not ledger.authorize_attempt(item.id, capability_id=item.capability_id,
                                        role="researcher", attempt=1)
    assert ledger.get(item.id).capability_state == "expired"


def test_local_revocation_invalidates_the_active_grant(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    assert ledger.revoke(item.id, attempt=1)
    assert not ledger.authorize_attempt(item.id, capability_id=item.capability_id,
                                        role="researcher", attempt=1)
    assert ledger.get(item.id).capability_state == "revoked"


def test_supervisor_refuses_a_revoked_grant_before_brokering_worker_ipc(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    assert ledger.revoke(item.id, attempt=1)
    supervisor = LocalWorkerSupervisor(object(), {}, delegation_ledger=ledger)
    request = WorkerRequest(role="researcher", task="x", run_id="run", delegation_id=item.id,
                            capability_id=item.capability_id)
    assert not supervisor._authorized(request, 1)


def test_real_worker_stops_when_parent_revokes_between_ipc_frames(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1

    class RevokingRouter:
        async def complete(self, *_args, **_kwargs):
            assert ledger.revoke(item.id, attempt=1)
            return CompletionResult(text="", model="test", tool_calls=[ToolCall(
                id="after-revoke", name="query_memory", arguments="{}",
            )])

    supervisor = LocalWorkerSupervisor(RevokingRouter(), {}, delegation_ledger=ledger)
    result = asyncio.run(supervisor.execute(
        "attempt a tool after revocation", "researcher", run_id="run", delegation_id=item.id,
        capability_id=item.capability_id, attempt=1,
    ))
    assert result.content == "[subagent failed: worker unavailable]"


def test_expired_parent_fails_closed_for_an_active_child(tmp_path):
    now = [100.0]
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13", clock=lambda: now[0])
    parent = ledger.create(parent_run_id="root", child_run_id="coordinator", role="coordinator")
    assert ledger.claim(parent.id) == 1
    child = ledger.create(parent_run_id="coordinator", child_run_id="research", role="researcher",
                          parent_delegation_id=parent.id)
    assert ledger.claim(child.id) == 1
    now[0] = parent.capability_deadline_ts + 1
    assert not ledger.authorize_attempt(child.id, capability_id=child.capability_id,
                                        role="researcher", attempt=1)


def test_revoked_parent_cannot_issue_a_new_child(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m13")
    parent = ledger.create(parent_run_id="root", child_run_id="coordinator", role="coordinator")
    assert ledger.claim(parent.id) == 1
    assert ledger.revoke(parent.id, attempt=1)
    import pytest

    with pytest.raises(ValueError, match="capability"):
        ledger.create(parent_run_id="coordinator", child_run_id="research", role="researcher",
                      parent_delegation_id=parent.id)
