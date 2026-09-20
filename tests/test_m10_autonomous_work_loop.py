"""M10 durable autonomous-goal control loop integration tests."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from starlette.testclient import TestClient

from hive.autonomy.goals import BLOCKED, COMPLETED, EXECUTING, REPLANNING, GoalLedger
from hive.autonomy.heartbeat import Heartbeat
from hive.autonomy.tasks import TaskBoard
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.observability.incidents import IncidentLedger
from hive.runtime import HiveOS
from hive.surfaces import cli


@pytest.fixture(autouse=True)
def _machine_identity(monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m10-test-machine")


class _Router:
    async def complete(self, *args, **kwargs):
        raise AssertionError("goal gateway controls must not invoke a model")

    async def aclose(self):
        return None


def _goal_hive(tmp_path):
    db = tmp_path / "state.sqlite"
    board = TaskBoard(db)
    goals = GoalLedger(db)
    incidents = IncidentLedger(db)
    cfg = SimpleNamespace(
        max_concurrent_agents=1,
        state_db=db,
        worker_isolation="required",
    )
    hive = SimpleNamespace(
        config=cfg,
        task_board=board,
        goal_ledger=goals,
        goal_intents=SimpleNamespace(get=lambda _goal_id: "safe operator intent"),
        incident_ledger=incidents,
        memory=SimpleNamespace(prefetch=lambda _query: "safe context"),
        tools={"safe_tool": object()},
    )
    hive.planner = SimpleNamespace(
        plan=AsyncMock(return_value=[{"tool": "safe_tool", "args": {"token": "not-public"}}])
    )
    return hive, board, goals, incidents


def test_goal_loop_completes_only_after_durable_task_evidence(tmp_path):
    hive, board, goals, incidents = _goal_hive(tmp_path)
    try:
        goal = goals.create("Run a safe check with token=owner-secret")
        heartbeat = Heartbeat(hive)

        assert asyncio.run(heartbeat._plan_one_goal(context_hint="context")) == 1
        planned = goals.get(goal.goal_id)
        assert planned is not None and planned.status == EXECUTING
        task = board.get(planned.task_ids[0])
        assert task is not None and task.source == f"goal:{goal.goal_id}"
        assert task.payload["reason"] == "operator goal"
        assert "owner-secret" not in planned.summary

        board.claim(task.id)
        board.complete(task.id)
        result = heartbeat._evaluate_goals()
        assert result == {"completed": 1, "replanned": 0, "blocked": 0}
        assert goals.get(goal.goal_id).status == COMPLETED
        assert incidents.recent() == []
    finally:
        board.close()
        goals.close()
        incidents.close()


def test_goal_plan_batch_is_atomic_when_one_entry_is_invalid(tmp_path):
    board = TaskBoard(tmp_path / "state.sqlite")
    try:
        try:
            board.enqueue_many([
                {"kind": "tool", "payload": {"tool": "safe"}, "source": "goal:g"},
                {"kind": "tool", "payload": {"tool": "safe"}, "source": "goal:g", "max_attempts": 0},
            ])
        except ValueError:
            pass
        else:  # pragma: no cover - safety assertion above must reject the batch
            raise AssertionError("invalid batch entry was accepted")
        assert board.all() == []
    finally:
        board.close()


def test_cancelled_planning_claim_leaves_no_dispatchable_goal_tasks(tmp_path):
    board = TaskBoard(tmp_path / "state.sqlite")
    goals = GoalLedger(tmp_path / "state.sqlite")
    try:
        goal = goals.create("cancel during planner work")
        claim = goals.claim_planning(goal.goal_id)
        ids = board.enqueue_many([
            {"kind": "tool", "payload": {"tool": "safe"}, "source": f"goal:{goal.goal_id}",
             "idempotency_key": f"goal:{goal.goal_id}:generation:{claim.plan_generation}:task:1"},
        ])
        assert goals.cancel(goal.goal_id) is not None
        assert goals.begin_execution(goal.goal_id, ids, expected_generation=claim.plan_generation) is None
        for task_id in ids:
            assert board.cancel(task_id)
        assert board.due() == []
    finally:
        board.close()
        goals.close()


def test_goal_loop_stops_after_two_replans_and_creates_redacted_incident(tmp_path):
    hive, board, goals, incidents = _goal_hive(tmp_path)
    try:
        goal = goals.create("Repair a failing integration")
        heartbeat = Heartbeat(hive)
        for expected_replans in (1, 2):
            assert asyncio.run(heartbeat._plan_one_goal(context_hint="context")) == 1
            current = goals.get(goal.goal_id)
            assert current is not None and current.status == EXECUTING
            board.fail(current.task_ids[0], "token=private-failure")
            transition = heartbeat._evaluate_goals()
            assert transition["replanned"] == 1
            assert goals.get(goal.goal_id).status == REPLANNING
        assert asyncio.run(heartbeat._plan_one_goal(context_hint="context")) == 1
        current = goals.get(goal.goal_id)
        board.fail(current.task_ids[0], "another failure")
        transition = heartbeat._evaluate_goals()
        assert transition["blocked"] == 1
        assert goals.get(goal.goal_id).status == BLOCKED
        assert goals.get(goal.goal_id).replan_count == 2
        rendered = str(incidents.recent())
        assert "private-failure" not in rendered
        assert incidents.recent()[0]["source"] == "goal"
    finally:
        board.close()
        goals.close()
        incidents.close()


def test_goal_gateway_makes_agent_token_read_only_and_redacts_goal_tasks(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        approver_key="approver-secret",
    )
    hive = HiveOS.build(cfg, router=_Router())
    intents: dict[str, str] = {}
    hive.goal_intents = SimpleNamespace(
        put=lambda goal_id, intent: intents.__setitem__(goal_id, intent),
        get=lambda goal_id: intents.get(goal_id),
    )
    agent_headers = {"X-Hive-Token": cfg.secret}
    approver_headers = {"X-Hive-Token": "approver-secret"}
    with TestClient(create_app(hive)) as client:
        denied = client.post("/goals", headers=agent_headers, json={"summary": "safe goal"})
        assert denied.status_code == 401
        created = client.post(
            "/goals", headers=approver_headers,
            json={"summary": "Check token=owner-secret without exposing it"},
        )
        assert created.status_code == 200
        goal_id = created.json()["goal_id"]
        shown = client.get(f"/goals/{goal_id}", headers=agent_headers)
        assert shown.status_code == 200
        assert shown.json()["summary"] == "operator-managed goal"
        assert "owner-secret" not in str(shown.json())
        task_id = hive.task_board.enqueue(
            "tool", {"tool": "safe_tool", "args": {"token": "goal-task-secret"}},
            source=f"goal:{goal_id}",
        )
        listed_tasks = client.get("/tasks", headers=agent_headers)
        assert listed_tasks.status_code == 200
        assert "goal-task-secret" not in str(listed_tasks.json())
        assert any(task["payload"] == {"goal_managed": True}
                   for task in listed_tasks.json()["tasks"])
        hive.task_board.fail(task_id, "token=goal-task-error")
        assert "goal-task-error" not in str(client.get("/tasks/failed", headers=agent_headers).json())
        assert "goal-task-error" not in str(client.get("/tasks/last-failed", headers=agent_headers).json())
        assert client.post(f"/goals/{goal_id}/cancel", headers=agent_headers).status_code == 401
        assert client.post(f"/goals/{goal_id}/cancel", headers=approver_headers).status_code == 200
    asyncio.run(hive.aclose())


def test_goal_cli_uses_approver_credential_only_for_mutations(tmp_path, monkeypatch, capsys):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        approver_key="approver-secret",
    )
    calls = []

    def request(_cfg, method, path, *, credential, body=None, approver=False):
        calls.append((method, path, credential, body, approver))
        return {"goal_id": "goal-1", "summary": "safe", "status": "open", "task_ids": []}

    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli, "_gateway_request", request)
    assert cli.main(["goals", "create", "safe", "goal"]) == 0
    assert calls == [("POST", "/goals", "approver-secret", {"summary": "safe goal"}, True)]
    capsys.readouterr()
    calls.clear()
    assert cli.main(["goals"]) == 0
    assert calls == [("GET", "/goals", cfg.secret, None, False)]
