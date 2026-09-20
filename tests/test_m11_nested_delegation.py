"""M11 — a coordinator creates a fenced child through the existing supervisor seam."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from hive.agents.base import AgentResult
from hive.agents.delegations import DelegationLedger
from hive.agents.worker_protocol import WorkerRequest
from hive.agents.worker_supervisor import LocalWorkerSupervisor, _TrustedTurnState
from hive.core.config import HiveConfig
from hive.core.run_context import bind_delegation_id, bind_run_id
from hive.core.types import Message, Role, ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.builtins import DelegateToSpecialist
from hive.tools.executor import ToolExecutor


class _Worker:
    def __init__(self, tool: DelegateToSpecialist, role: str) -> None:
        self._tool = tool
        self._role = role

    async def execute(self, task: str, role: str, *, run_id: str, delegation_id: str) -> AgentResult:
        assert role == self._role
        if role == "coordinator":
            with bind_run_id(run_id), bind_delegation_id(delegation_id):
                child = await self._tool.execute(agent="researcher", task="safe research")
            return AgentResult(content=child.content)
        return AgentResult(content="research complete")


def test_coordinator_creates_a_durable_child_without_authority_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m11-local")
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m11-local")
    tool = DelegateToSpecialist(delegation_ledger=ledger)
    workers = {role: _Worker(tool, role) for role in ("coordinator", "researcher")}
    tool.set_worker_resolver(workers.get)

    with bind_run_id("hive-root-run"):
        result = asyncio.run(tool.execute(agent="coordinator", task="coordinate a review"))

    assert result.success and result.content == "research complete"
    roots = ledger.for_parent("hive-root-run")
    assert len(roots) == 1 and roots[0].role == "coordinator"
    tree = ledger.tree(roots[0].id)
    assert tree is not None and tree["node_count"] == 2
    child = tree["root"]["children"][0]
    assert child["role"] == "researcher"
    assert child["depth"] == 1
    assert "coordinate a review" not in str(tree)


def test_non_coordinator_cannot_create_a_child(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m11-local")
    ledger = DelegationLedger(tmp_path / "state.sqlite", machine_identity="m11-local")
    root = ledger.create(parent_run_id="root", child_run_id="research", role="researcher")
    assert ledger.claim(root.id) == 1
    tool = DelegateToSpecialist(delegation_ledger=ledger)

    async def nested_attempt():
        with bind_run_id("research"), bind_delegation_id(root.id):
            return await tool.execute(agent="researcher", task="must not create")

    result = asyncio.run(nested_attempt())
    assert not result.success
    assert result.content.startswith("[delegate error:")
    assert ledger.children(root.id) == []


class _NestedRouter:
    """Deterministic supervisor-owned model for a real worker-process proof."""

    async def complete(self, messages, *, system="", **_kwargs):
        if "specialist:researcher" in system:
            return CompletionResult(text="research complete", model="test")
        if "specialist:coordinator" in system:
            if any(message.role.value == "tool" for message in messages):
                return CompletionResult(text="coordinator complete", model="test")
            return CompletionResult(
                text="", model="test",
                tool_calls=[ToolCall(
                    id="delegate-child", name="delegate_to_specialist",
                    arguments=json.dumps({"agent": "researcher", "task": "safe research"}),
                )],
            )
        raise AssertionError(f"unexpected worker prompt: {system!r}")

    async def aclose(self):
        return None


def test_real_local_worker_process_can_create_a_fenced_child(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m11-real-worker")
    monkeypatch.setenv("HIVE_PRODUCTION", "false")
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        host="127.0.0.1", secret="m11-test-secret", worker_isolation="preferred",
    )
    hive = HiveOS.build(cfg, router=_NestedRouter())
    try:
        async def run():
            with bind_run_id("m11-real-root"):
                return await hive.tools["delegate_to_specialist"].execute(
                    agent="coordinator", task="coordinate safe research",
                )

        result = asyncio.run(run())
        assert result.success and result.content == "coordinator complete"
        roots = hive.delegation_ledger.for_parent("m11-real-root")
        assert len(roots) == 1 and roots[0].role == "coordinator"
        tree = hive.delegation_ledger.tree(roots[0].id)
        assert tree is not None and tree["node_count"] == 2
        assert tree["root"]["children"][0]["role"] == "researcher"
    finally:
        asyncio.run(hive.aclose())


def test_supervisor_enforces_persisted_child_budget_not_its_global_default():
    supervisor = LocalWorkerSupervisor(_NestedRouter(), {}, max_iterations=30, max_per_tool=50)
    request = WorkerRequest(
        role="researcher", task="x", run_id="run", delegation_id="delegation",
        max_iterations=1, max_per_tool=1,
    )
    state = _TrustedTurnState(messages=[Message(role=Role.USER, content="x")], pending={}, model_calls=2)

    reply = asyncio.run(supervisor._handle(
        {"type": "model", "tools": []}, request, ToolExecutor({}), state, frozenset(),
    ))

    assert reply == {"error": "WorkerProtocolError"}
