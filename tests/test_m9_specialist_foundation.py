"""M9 foundation: enforceable specialist profiles and durable delegation state."""
from __future__ import annotations

import asyncio

import pytest

from hive.agents.base import AgentResult, BaseAgent
from hive.agents.delegate import register_agent
from hive.agents.delegations import COMPLETED, FAILED, RUNNING, DelegationLedger
from hive.agents.profiles import specialist_profile, specialist_profiles
from hive.core.events import EventBus
from hive.core.run_context import bind_run_id
from hive.tools.builtins import DelegateToSpecialist


def test_profiles_are_closed_and_coder_requires_independent_review():
    assert {profile.name for profile in specialist_profiles()} == {
        "researcher", "reviewer", "security-reviewer", "memory-keeper", "coder",
    }
    assert specialist_profile("coder").requires_independent_review
    with pytest.raises(ValueError, match="unknown specialist role"):
        specialist_profile("release-manager")


def test_ledger_persists_and_fences_terminal_result(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    attempt = ledger.claim(item.id)
    assert attempt == 1
    assert ledger.get(item.id).state == RUNNING
    assert ledger.finish(item.id, attempt=attempt, success=True, summary="token=secret")
    completed = ledger.get(item.id)
    assert completed.state == COMPLETED
    assert "secret" not in completed.safe_summary
    assert not ledger.finish(item.id, attempt=attempt, success=False, summary="late")
    ledger.close()


def test_ledger_failure_is_durable_and_parent_query_is_bounded(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="reviewer")
    attempt = ledger.claim(item.id)
    assert ledger.finish(item.id, attempt=attempt, success=False, summary="failed")
    assert ledger.get(item.id).state == FAILED
    assert [row.id for row in ledger.for_parent("parent")] == [item.id]
    ledger.close()


def test_real_delegate_creates_and_finishes_durable_record(tmp_path):
    class _Leaf(BaseAgent):
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="done")

    register_agent("researcher", lambda: _Leaf())
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    tool = DelegateToSpecialist(bus=EventBus(), delegation_ledger=ledger)

    async def run():
        with bind_run_id("parent"):
            return await tool.execute(agent="researcher", task="safe research")

    assert asyncio.run(run()).content == "done"
    rows = ledger.for_parent("parent")
    assert len(rows) == 1
    assert rows[0].role == "researcher"
    assert rows[0].state == COMPLETED
    ledger.close()


def test_unknown_role_is_rejected_before_ledger_write(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    result = asyncio.run(DelegateToSpecialist(delegation_ledger=ledger).execute(
        agent="release-manager", task="do not run",
    ))
    assert not result.success
    assert "unknown specialist role" in result.content
    assert ledger.for_parent("") == []
    ledger.close()
