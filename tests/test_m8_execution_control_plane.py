"""M8 execution control-plane projections remain bounded and content-safe."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

from hive.core.events import Event, EventType
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.observability.runs import RunLedger
from hive.runtime import HiveOS
from hive.surfaces import cli
from starlette.testclient import TestClient


class _Router:
    async def complete(self, *args, **kwargs):
        raise AssertionError("execution-observability reads must not invoke a model")

    async def aclose(self) -> None:
        return None


def _operator_event(run_id: str, event_type: str, **fields: object) -> dict[str, object]:
    return {
        "version": 1,
        "type": event_type,
        "run_id": run_id,
        "session_id": "private-session",
        "sequence": 1,
        "timestamp": 10.0,
        **fields,
    }


def test_snapshot_exposes_safe_current_execution_state(tmp_path):
    ledger = RunLedger(tmp_path / "state.sqlite", clock=lambda: 20.0)
    try:
        ledger.begin("parent", kind="conversation", session_id="private-session")
        ledger.record_operator_event(_operator_event("parent", "model_decision", tool_calls=[{"name": "shell"}]))
        ledger.record_operator_event(_operator_event("parent", "tool_call_start", name="shell"))
        ledger.record_operator_event(_operator_event(
            "parent", "tool_call_end", name="shell", status="pending", arguments="secret-value",
        ))
        snapshot = ledger.snapshot("parent")
    finally:
        ledger.close()
    assert snapshot is not None
    assert snapshot["phase"] == "waiting_approval"
    assert snapshot["active_tool"] == ""
    assert snapshot["tool_event_count"] == 1
    assert "private-session" not in str(snapshot)
    assert "secret-value" not in str(snapshot)


def test_public_events_are_cursorable_and_exclude_raw_run_events(tmp_path):
    ledger = RunLedger(tmp_path / "state.sqlite")
    try:
        ledger.begin("run", kind="conversation")
        ledger.record_event(Event(EventType.TOOL_CALL_START, {"run_id": "run", "arguments": "secret-value"}))
        ledger.record_operator_event(_operator_event("run", "tool_call_start", name="safe-tool"))
        ledger.record_operator_event(_operator_event("run", "tool_call_end", name="safe-tool", status="ok"))
        first = ledger.public_events("run", limit=1)
        second = ledger.public_events("run", after_id=first[0]["id"])
    finally:
        ledger.close()
    assert len(first) == 1 and len(second) == 1
    assert first[0]["id"] < second[0]["id"]
    assert second[0]["type"] == "tool_call_end"
    assert "secret-value" not in str(first + second)
    assert "private-session" not in str(first + second)


def test_snapshot_uses_the_latest_bounded_operator_window(tmp_path):
    ledger = RunLedger(tmp_path / "state.sqlite")
    try:
        ledger.begin("run", kind="conversation")
        for index in range(501):
            ledger.record_operator_event(_operator_event("run", "tool_call_end", name=f"old-{index}", status="ok"))
        ledger.record_operator_event(_operator_event("run", "tool_call_start", name="current-tool"))
        snapshot = ledger.snapshot("run")
    finally:
        ledger.close()
    assert snapshot is not None and snapshot["phase"] == "executing_tool"
    assert snapshot["active_tool"] == "current-tool"


def test_tree_is_recursive_bounded_and_hides_child_error_details(tmp_path):
    ledger = RunLedger(tmp_path / "state.sqlite")
    try:
        ledger.begin("root", kind="conversation")
        ledger.record_event(Event(EventType.SUBAGENT_STARTED, {"run_id": "root", "subagent_run_id": "child"}))
        ledger.record_event(Event(EventType.SUBAGENT_STARTED, {"run_id": "child", "subagent_run_id": "grandchild"}))
        ledger.finish("child", state="error", error="secret failure detail")
        tree = ledger.tree("root", max_depth=1)
    finally:
        ledger.close()
    assert tree is not None and tree["truncated"] is True
    assert tree["root"]["children"][0]["run_id"] == "child"
    assert tree["root"]["children"][0]["children"] == []
    assert "secret failure detail" not in str(tree)


def test_snapshot_marks_only_a_locally_recovered_interruption(tmp_path):
    ledger = RunLedger(
        tmp_path / "state.sqlite", hostname="local", process_id=123,
        process_is_alive=lambda _pid: False,
    )
    try:
        ledger.begin("stale", kind="conversation")
        ledger.begin("normal-cancel", kind="conversation")
        ledger.finish("normal-cancel", state="cancelled", error="operator cancelled")
        assert ledger.recover_interrupted("stale") == 1
        stale = ledger.snapshot("stale")
        normal = ledger.snapshot("normal-cancel")
    finally:
        ledger.close()
    assert stale is not None and stale["interrupted_local"] is True
    assert normal is not None and normal["interrupted_local"] is False


def test_terminal_run_views_render_only_safe_execution_projection(tmp_path, monkeypatch, capsys):
    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), state_db=tmp_path / "state.sqlite")
    ledger = RunLedger(cfg.state_db)
    try:
        ledger.begin("run", kind="conversation", session_id="private-session")
        ledger.record_event(Event(EventType.SUBAGENT_STARTED, {"run_id": "run", "subagent_run_id": "child"}))
        ledger.finish("run", state="error", error="secret failure detail")
    finally:
        ledger.close()
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))

    assert cli.main(["runs", "show", "run"]) == 0
    shown = capsys.readouterr().out
    assert "phase" in shown and "private-session" not in shown and "secret failure detail" not in shown
    assert cli.main(["runs", "tree", "run"]) == 0
    tree = capsys.readouterr().out
    assert "child" in tree and "private-session" not in tree
    assert cli.main(["status", "--live"]) == 1  # inherited test config warnings
    status = capsys.readouterr().out
    assert "executions" in status and "private-session" not in status


def test_gateway_execution_reads_are_authenticated_cursorable_and_content_safe(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="agent-key", host="127.0.0.1", production_mode=False, autonomy_enabled=False,
    )
    hive = HiveOS.build(cfg, router=_Router())
    try:
        hive.run_ledger.begin("run", kind="conversation", session_id="private-session")
        hive.run_ledger.record_operator_event(_operator_event("run", "tool_call_start", name="safe-tool"))
        hive.run_ledger.record_event(Event(EventType.TOOL_CALL_END, {"run_id": "run", "result": "secret-value"}))
        with TestClient(create_app(hive)) as client:
            assert client.get("/runs/run").status_code == 401
            snapshot = client.get("/runs/run", headers={"X-Hive-Token": "agent-key"})
            events = client.get("/runs/run/events", headers={"X-Hive-Token": "agent-key"})
            tree = client.get("/runs/run/tree", headers={"X-Hive-Token": "agent-key"})
            status = client.get("/execution/status", headers={"X-Hive-Token": "agent-key"})
    finally:
        hive.run_ledger.close()
    assert snapshot.status_code == 200 and snapshot.json()["active_tool"] == "safe-tool"
    assert events.status_code == 200 and events.json()["next_after_id"] > 0
    assert tree.status_code == 200 and tree.json()["root"]["run_id"] == "run"
    assert status.status_code == 200 and status.json()["executions"]["running"] == 1
    combined = f"{snapshot.text}{events.text}{tree.text}"
    assert "private-session" not in combined and "secret-value" not in combined


def test_terminal_gateway_execution_modes_remain_read_only_and_safe(tmp_path, monkeypatch, capsys):
    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), secret="agent-key", host="127.0.0.1")
    payloads = {
        "/runs/run": {"state": "running", "phase": "executing_tool", "kind": "conversation",
                       "parent_run_id": "", "active_tool": "shell", "child_runs": {"total": 0}},
        "/runs/run/tree": {"root": {"run_id": "run", "state": "running", "kind": "conversation",
                                       "phase": "executing_tool", "children": []}, "truncated": False},
        "/execution/status": {"executions": {"running": 1, "ok": 2, "error": 0, "cancelled": 0}},
    }
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli, "_gateway_request", lambda _cfg, _method, path, **_kwargs: payloads.get(path))

    assert cli.main(["runs", "show", "run", "--gateway"]) == 0
    assert "executing_tool" in capsys.readouterr().out
    assert cli.main(["runs", "tree", "run", "--gateway"]) == 0
    assert "HiveOS Run Tree (gateway)" in capsys.readouterr().out
    assert cli.main(["status", "--live", "--gateway"]) == 1  # inherited config warnings
    assert "executions" in capsys.readouterr().out


def test_real_terminal_process_renders_safe_durable_snapshot(tmp_path):
    state_db = tmp_path / "state.sqlite"
    ledger = RunLedger(state_db)
    try:
        ledger.begin("real-process-run", kind="conversation", session_id="private-session")
        ledger.record_operator_event(_operator_event("real-process-run", "tool_call_start", name="safe-tool"))
        ledger.finish("real-process-run", state="error", error="secret failure detail")
    finally:
        ledger.close()
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "HIVE_STATE_DB": str(state_db),
        "HIVE_SECRET": "change_me",
        "HIVE_PRODUCTION": "false",
        "PYTHONPATH": str(repo_root / "src"),
        "NO_COLOR": "1",
    }
    process = subprocess.run(
        [sys.executable, "-c", "from hive.surfaces.cli import main; raise SystemExit(main(['runs', 'show', 'real-process-run']))"],
        cwd=repo_root, env=env, text=True, capture_output=True, timeout=20, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "HiveOS Run: real-process-run" in process.stdout
    assert "active tool : safe-tool" in process.stdout
    assert "private-session" not in process.stdout
    assert "secret failure detail" not in process.stdout
