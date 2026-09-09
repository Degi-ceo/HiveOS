"""M1 #126: one correlation id spans an autonomous heartbeat run."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hive.autonomy.heartbeat import Heartbeat
from hive.autonomy.tasks import TaskBoard
from hive.core.events import EventBus, EventType
from hive.core.approval import gate
from hive.core.approval_enhancements import ApprovalGateEnhancements, enhance
from hive.core.config import HiveConfig
from hive.core.learning.tracer import Tracer
from hive.core.run_context import bind_run_id
from hive.core.safety_state import SafetyStateStore
from hive.core.self_mod import SelfModifier
from hive.core.types import ToolResult
from hive.llm.adapters.base import CompletionResult
from hive.observability.audit import AuditLog
from hive.observability.persistence import ObservabilityLedger
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.executor import ToolExecutor
from hive.runtime import HiveOS


class _OkTool(BaseTool):
    spec = ToolSpec(name="correlated_ok", description="Return a deterministic result")

    async def execute(self, **params: object) -> ToolResult:
        return ToolResult(tool_name=self.spec.name, content="ok")


class _DangerousTool(_OkTool):
    spec = ToolSpec(
        name="correlated_danger", description="Require approval", dangerous=True,
    )


class _FakeGate:
    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}

    def request(self, tool: str, args: dict, reason: str, kind: str = "danger") -> str:
        approval_id = "approval-1"
        self._pending[approval_id] = {
            "id": approval_id, "tool": tool, "args": args,
            "reason": reason, "kind": kind,
        }
        return approval_id

    def resolve(self, approval_id: str, approved: bool) -> dict | None:
        return self._pending.pop(approval_id, None)

    def pending(self) -> list[dict]:
        return list(self._pending.values())

    def is_dangerous(self, name: str, args: dict) -> bool:
        return False


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kwargs):
        return CompletionResult(text="ok", model="fake")

    async def aclose(self) -> None:
        return None


def _selfmod_runner():
    async def run(cmd, cwd=None):
        command = " ".join(cmd) if isinstance(cmd, list) else cmd
        if command.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if command.startswith("git diff --name-only"):
            return 0, "src/hive/example.py\n"
        if command.startswith("git ls-files --others"):
            return 0, ""
        if command.startswith("git status --porcelain"):
            return 0, " M src/hive/example.py\n"
        return 0, "ok"

    return run


def test_existing_task_and_audit_databases_migrate_run_id(tmp_path):
    task_db = tmp_path / "tasks.sqlite"
    board = TaskBoard(task_db)
    task_id = board.enqueue("tool", {"tool": "legacy"})
    board.close()
    with sqlite3.connect(task_db) as db:
        db.execute("ALTER TABLE hive_tasks RENAME TO legacy_tasks")
        db.execute(
            "CREATE TABLE hive_tasks AS SELECT id, kind, payload, state, created_ts, "
            "updated_ts, scheduled_for, source, attempts, last_error FROM legacy_tasks"
        )
        db.execute("DROP TABLE legacy_tasks")
    migrated_board = TaskBoard(task_db)
    assert migrated_board.get(task_id).run_id == ""

    audit_db = tmp_path / "audit.sqlite"
    with sqlite3.connect(audit_db) as db:
        db.execute(
            "CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
            "tool TEXT, status TEXT, approved INTEGER, error TEXT, args TEXT)"
        )
        db.execute(
            "INSERT INTO audit_log(ts, tool, status, approved, error, args) "
            "VALUES(1, 'legacy', 'ok', 0, '', '{}')"
        )
    audit = AuditLog(audit_db)
    assert audit.recent(1)[0]["run_id"] == ""


def test_concurrent_ticks_receive_isolated_run_ids():
    events = EventBus(record_history=True)
    hive = SimpleNamespace(
        config=SimpleNamespace(max_concurrent_agents=1, autonomy_enabled=False),
        events=events,
    )
    heartbeat = Heartbeat(hive)

    async def run_both() -> list[dict]:
        return list(await asyncio.gather(heartbeat.tick(1.0), heartbeat.tick(2.0)))

    summaries = asyncio.run(run_both())
    run_ids = {summary["run_id"] for summary in summaries}
    assert len(run_ids) == 2
    for run_id in run_ids:
        matching = [
            event for event in events.history()
            if event.data.get("run_id") == run_id
        ]
        assert [event.event_type for event in matching] == [
            EventType.AGENT_TICK_START, EventType.AGENT_TICK_END,
        ]


def test_simulated_tick_propagates_run_id_through_task_tool_selfmod_and_pr(tmp_path):
    state_db = tmp_path / "state.sqlite"
    board = TaskBoard(state_db)
    failed_id = board.enqueue("tool", {"tool": "old_failure"}, source="test")
    assert board.claim(failed_id)
    board.fail(failed_id, "historical failure")
    task_id = board.enqueue("tool", {"tool": "correlated_ok"}, source="test")

    audit = AuditLog(tmp_path / "audit.sqlite")
    tracer = Tracer(state_db)
    events = EventBus(record_history=True)
    executor = ToolExecutor(
        {"correlated_ok": _OkTool()}, audit=audit.record, tracer=tracer,
        events=events,
    )
    opened: dict[str, str] = {}
    ledger = ObservabilityLedger(state_db)

    async def open_pr(branch: str, title: str, body: str) -> str:
        opened.update(branch=branch, title=title, body=body)
        return "https://github.com/Degi-ceo/HiveOS/pull/999"

    modifier = SelfModifier(
        repo_root=str(tmp_path), run=_selfmod_runner(), open_pr=open_pr,
        audit=audit.record, history_store=ledger,
    )

    async def apply_change(_worktree: str) -> list[str]:
        return ["src/hive/example.py"]

    selfmod_results: list[dict] = []

    async def self_improve(_symptom: str, **_kwargs: object) -> list[dict]:
        result = await modifier.propose("repair", "repair the failure", apply_change)
        selfmod_results.append(result)
        return [result]

    hive = SimpleNamespace(
        config=SimpleNamespace(
            max_concurrent_agents=1,
            heartbeat_sec=900,
            autonomy_enabled=True,
            autonomous_selfmod_enabled=True,
            selfmod_failure_threshold=1,
            selfmod_failure_cooldown_sec=0.0,
            selfmod_proactive_interval=0,
            heartbeat_proactive_interval_sec=0,
        ),
        events=events,
        task_board=board,
        tool_executor=executor,
        cron=MagicMock(due_and_enqueue=MagicMock(return_value=0)),
        commitments=MagicMock(due_and_enqueue=MagicMock(return_value=0)),
        memory=MagicMock(prefetch=MagicMock(return_value="context")),
        planner=MagicMock(plan=AsyncMock(return_value=[])),
        budgeter=MagicMock(
            daily_spend_status=MagicMock(return_value={"hard_cap_reached": False}),
            refresh=AsyncMock(),
        ),
        consolidate=AsyncMock(return_value=0),
        curate=MagicMock(return_value={"transitions": []}),
        curate_umbrellas=AsyncMock(),
        self_improve_from_symptom=self_improve,
        self_diagnose=AsyncMock(return_value={"improvement_outcomes": []}),
    )

    summary = asyncio.run(Heartbeat(hive, goals=["repair safely"]).tick(now=1000.0))
    run_id = summary["run_id"]

    assert summary["dispatched"] == 1
    assert board.get(task_id).run_id == run_id
    rows = audit.search(run_id=run_id, limit=20)
    assert {row["tool"] for row in rows} >= {"correlated_ok", "self_mod"}
    trace = tracer.recent_traces(limit=1)[0]
    assert trace.run_id == run_id
    assert trace.session_id == "unknown"

    result = selfmod_results[0]
    assert result["run_id"] == run_id
    assert run_id[:8] in result["branch"] == opened["branch"]
    assert f"Run ID: `{run_id}`" in opened["body"]
    assert ledger.find_selfmod_run_id(branch=opened["branch"]) == run_id
    assert ledger.find_selfmod_run_id(
        pr_url="https://github.com/Degi-ceo/HiveOS/pull/999"
    ) == run_id

    tick_events = [
        event for event in events.history()
        if event.event_type in {EventType.AGENT_TICK_START, EventType.AGENT_TICK_END}
    ]
    assert len(tick_events) == 2
    assert all(event.data["run_id"] == run_id for event in tick_events)
    tool_events = [
        event for event in events.history()
        if event.event_type is EventType.TOOL_CALL_END
    ]
    assert tool_events and tool_events[0].data["run_id"] == run_id


def test_runtime_build_wires_tick_task_audit_and_learning_trace(tmp_path):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    object.__setattr__(config, "autonomy_enabled", True)
    object.__setattr__(config, "approver_key", "test-approver-key")
    hive = HiveOS.build(config, router=_Router())
    target = tmp_path / "correlation.txt"
    target.write_text("ok", encoding="utf-8")
    task_id = hive.task_board.enqueue(
        "tool", {"tool": "read_file", "args": {"path": str(target)}},
        source="test",
    )

    summary = asyncio.run(Heartbeat(hive).tick(now=1000.0))
    run_id = summary["run_id"]

    assert hive.task_board.get(task_id).run_id == run_id
    assert hive.audit_log.search(run_id=run_id)[0]["tool"] == "read_file"
    traces = hive.learning_tracer.recent_traces(run_id=run_id, limit=10)
    assert len(traces) == 1
    assert traces[0].tool == "read_file"


def test_runtime_build_wires_approval_resolution_to_its_event_bus(tmp_path):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(config, router=_Router())
    resolved = []
    hive.events.subscribe(EventType.APPROVAL_RESOLVED, resolved.append)

    with bind_run_id("approval-run"):
        approval_id = gate.request("deploy", {"target": "test"}, "correlate")
        enhance.audit_request(approval_id)
    item, outcome = enhance.resolve_with_outcome(
        approval_id, approved=False, decided_by="human:test",
    )

    assert item is not None
    assert outcome is not None and outcome.value == "rejected"
    assert len(resolved) == 1
    assert resolved[0].data == {
        "approval_id": approval_id,
        "outcome": "rejected",
        "tool": "deploy",
        "run_id": "approval-run",
    }


def test_gateway_audit_search_accepts_run_id(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    audit.record({"tool": "one", "status": "ok", "run_id": "run-one"})
    audit.record({"tool": "two", "status": "ok", "run_id": "run-two"})
    assert [row["tool"] for row in audit.search(run_id="run-one")] == ["one"]


def test_run_chain_is_complete_and_chronological(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    for index in range(55):
        audit.record({"tool": f"step-{index}", "status": "ok", "run_id": "long-run"})
    audit.record({"tool": "other", "status": "ok", "run_id": "other-run"})
    chain = audit.run_chain("long-run")
    assert len(chain) == 55
    assert chain[0]["tool"] == "step-0"
    assert chain[-1]["tool"] == "step-54"


def test_pending_approval_is_not_traced_as_a_failure(tmp_path):
    gate = _FakeGate()
    tracer = Tracer(tmp_path / "learning.sqlite")
    executor = ToolExecutor(
        {"correlated_danger": _DangerousTool()}, gate=gate, tracer=tracer,
    )

    pending = asyncio.run(executor.execute("correlated_danger", run_id="run-pending"))
    assert pending.approval_id == "approval-1"
    assert tracer.recent_traces(limit=10) == []

    completed = asyncio.run(executor.execute_approved(
        "correlated_danger", {}, run_id="run-pending",
    ))
    assert completed.result.content == "ok"
    trace = tracer.recent_traces(limit=1)[0]
    assert trace.outcome == "ok"
    assert trace.run_id == "run-pending"


def test_approval_sidecar_restores_run_id_after_restart(tmp_path):
    state = SafetyStateStore(tmp_path / "state.sqlite")
    first_gate = _FakeGate()
    first = ApprovalGateEnhancements(first_gate, state_store=state)
    approval_id = first_gate.request("correlated_danger", {}, "reason")
    first.audit_request(approval_id, requested_at=10.0, run_id="run-before-restart")

    restored_gate = _FakeGate()
    restored = ApprovalGateEnhancements(
        restored_gate, state_store=SafetyStateStore(tmp_path / "state.sqlite"),
        clock=lambda: 11.0,
    )
    item, outcome = restored.resolve_with_outcome(approval_id, True)
    assert outcome.value == "approved"
    assert item["run_id"] == "run-before-restart"


def test_audit_integrity_covers_nonempty_run_id(tmp_path):
    path = tmp_path / "audit.sqlite"
    audit = AuditLog(path)
    audit.record({"tool": "one", "status": "ok", "run_id": "original-run"})
    audit.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_log SET run_id='tampered-run' WHERE id=1")
    assert AuditLog(path).verify_integrity()["valid"] is False


def test_legacy_learning_trace_schema_migrates_separate_run_id(tmp_path):
    path = tmp_path / "learning.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE learning_traces(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, session_id TEXT NOT NULL, tool TEXT NOT NULL, "
            "args_json TEXT NOT NULL DEFAULT '{}', outcome TEXT NOT NULL, "
            "latency_ms REAL NOT NULL DEFAULT 0, error_class TEXT, error_message TEXT)"
        )
    tracer = Tracer(path)
    tracer.record(
        tool="one", outcome="ok", session_id="conversation-7", run_id="run-7",
    )
    row = tracer.recent_traces(run_id="run-7", limit=1)[0]
    assert row.session_id == "conversation-7"
    assert row.run_id == "run-7"


def test_inference_ledger_prefers_bound_tick_run_with_runtime_fallback(tmp_path):
    path = tmp_path / "telemetry.sqlite"
    ledger = ObservabilityLedger(path, run_id="runtime-fallback")
    with bind_run_id("heartbeat-run"):
        ledger.record_inference({"model": "m", "input_tokens": 1})
    ledger.record_inference({"model": "m", "input_tokens": 1})
    ledger.close()

    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT run_id FROM telemetry ORDER BY id").fetchall()
    assert rows == [("heartbeat-run",), ("runtime-fallback",)]
