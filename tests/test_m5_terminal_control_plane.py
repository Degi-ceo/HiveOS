"""M5 terminal operator controls: credential boundary, bounded queue actions, recovery."""
from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import patch

from hive.autonomy.tasks import FAILED, PENDING, RUNNING, TaskBoard
from hive.core.events import Event, EventType
from hive.observability.runs import RunLedger
from hive.surfaces import cli


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self._payload


def _operator_env(monkeypatch, tmp_path, *, approver: str = "approver-key",
                  autonomy: str = "false") -> None:
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("HIVE_SECRET", "agent-key")
    monkeypatch.setenv("HIVE_APPROVER_KEY", approver)
    monkeypatch.setenv("HIVE_AUTONOMY_ENABLED", autonomy)
    monkeypatch.setenv("HIVE_HOST", "0.0.0.0")
    monkeypatch.setenv("HIVE_PORT", "18088")


def test_terminal_approval_uses_out_of_band_key_not_agent_token(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    captured = {}

    def _urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["token"] = request.get_header("X-hive-token")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        assert timeout == 5
        return _Response({"executed": False, "status": "approved"})

    with patch("urllib.request.urlopen", side_effect=_urlopen):
        assert cli.main(["approvals", "decide", "approval-123", "approve"]) == 0

    assert captured == {
        "url": "http://127.0.0.1:18088/approvals/decide",
        "token": "approver-key",
        "body": {"approval_id": "approval-123", "approved": True},
    }
    output = capsys.readouterr().out
    assert "out_of_band" in output
    assert "agent-key" not in output
    assert "approver-key" not in output


def test_terminal_approval_supervised_fallback_warns(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path, approver="", autonomy="false")
    captured = {}

    def _urlopen(request, *, timeout):
        captured["token"] = request.get_header("X-hive-token")
        return _Response({"executed": False, "status": "rejected"})

    with patch("urllib.request.urlopen", side_effect=_urlopen):
        assert cli.main(["approvals", "decide", "approval-123", "reject"]) == 0

    assert captured["token"] == "agent-key"
    output = capsys.readouterr().out
    assert "Warning" in output
    assert "HIVE_APPROVER_KEY" in output
    assert "supervised_fallback" in output


def test_terminal_approval_never_falls_back_when_autonomy_enabled(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path, approver="", autonomy="true")
    with patch("urllib.request.urlopen") as urlopen:
        assert cli.main(["approvals", "decide", "approval-123", "approve"]) == 2
    urlopen.assert_not_called()
    assert "requires HIVE_APPROVER_KEY" in capsys.readouterr().out


def test_terminal_approval_refuses_non_local_gateway_before_sending_key(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HIVE_HOST", "gateway.example.test")
    with patch("urllib.request.urlopen") as urlopen:
        assert cli.main(["approvals", "decide", "approval-123", "approve"]) == 1
    urlopen.assert_not_called()
    assert "only be sent to a local gateway" in capsys.readouterr().out


def test_terminal_approval_list_refuses_non_local_gateway_before_sending_secret(
    tmp_path, monkeypatch, capsys,
):
    _operator_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HIVE_HOST", "gateway.example.test")
    with patch("urllib.request.urlopen") as urlopen:
        assert asyncio.run(cli._approvals()) == 1
    urlopen.assert_not_called()
    assert "gateway credential may only be sent to a local gateway" in capsys.readouterr().out


def test_terminal_approval_preserves_configured_credential_bytes(tmp_path, monkeypatch):
    _operator_env(monkeypatch, tmp_path, approver=" approver-key ")
    captured = {}

    def _urlopen(request, *, timeout):
        captured["token"] = request.get_header("X-hive-token")
        return _Response({"executed": False, "status": "rejected"})

    with patch("urllib.request.urlopen", side_effect=_urlopen):
        assert cli.main(["approvals", "decide", "approval-123", "reject"]) == 0
    assert captured["token"] == " approver-key "


def test_terminal_approval_list_accepts_gateway_id_shape(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    with patch.object(cli, "_gateway_request", return_value={
        "pending": [{"id": "gateway-approval-id", "tool": "deploy", "reason": "review"}],
        "pending_edits": 0,
    }):
        assert asyncio.run(cli._approvals()) == 0
    output = capsys.readouterr().out
    assert "gateway-" in output
    assert "[?]" not in output


def test_terminal_task_retry_retains_failure_context_until_completion(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    ledger = RunLedger(db)
    ledger.begin("run-task", kind="conversation")
    ledger.close()
    board = TaskBoard(db)
    task_id = board.enqueue("tool", {"command": "safe"}, run_id="run-task")
    assert board.claim(task_id)
    board.fail(task_id, "transient gateway timeout")
    board.close()

    assert cli.main(["tasks", "retry", str(task_id)]) == 0
    output = capsys.readouterr().out
    assert "Recovery context retained" in output
    assert "transient gateway timeout" in output

    board = TaskBoard(db)
    task = board.get(task_id)
    assert task is not None and task.state == PENDING
    assert task.last_error == "transient gateway timeout"
    assert board.claim(task_id)
    assert board.complete(task_id)
    assert board.get(task_id).last_error is None
    board.close()

    ledger = RunLedger(db)
    events = ledger.events("run-task")
    ledger.close()
    assert any(event["type"] == "operator.operator_action" for event in events)


def test_terminal_task_cancel_refuses_running_work(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    board = TaskBoard(db)
    task_id = board.enqueue("tool")
    assert board.claim(task_id)
    board.close()

    assert cli.main(["tasks", "cancel", str(task_id)]) == 2
    assert "running work is never interrupted" in capsys.readouterr().out
    board = TaskBoard(db)
    assert board.get(task_id).state == RUNNING
    board.close()


def test_terminal_task_cancel_changes_only_pending_work(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    board = TaskBoard(db)
    task_id = board.enqueue("tool", run_id="run-cancel")
    board.close()

    assert cli.main(["tasks", "cancel", str(task_id)]) == 0
    assert "Cancelled pending task" in capsys.readouterr().out
    board = TaskBoard(db)
    assert board.get(task_id).state == "cancelled"
    board.close()


def test_terminal_task_retry_refuses_exhausted_attempt_budget(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    board = TaskBoard(db)
    task_id = board.enqueue("tool", max_attempts=1)
    assert board.claim(task_id)
    board.fail(task_id, "terminal failure")
    board.close()

    assert cli.main(["tasks", "retry", str(task_id)]) == 2
    assert "exhausted its retry budget" in capsys.readouterr().out
    board = TaskBoard(db)
    assert board.get(task_id).state == FAILED
    board.close()


def test_terminal_task_show_redacts_payload_and_failure(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HIVE_TEST_SECRET", "m5-secret")
    db = tmp_path / "state.sqlite"
    board = TaskBoard(db)
    task_id = board.enqueue("tool", {"token": "m5-secret"}, run_id="run-show")
    assert board.claim(task_id)
    board.fail(task_id, "failure=m5-secret")
    board.close()

    assert cli.main(["tasks", "show", str(task_id)]) == 0
    output = capsys.readouterr().out
    assert "m5-secret" not in output
    assert "***REDACTED***" in output


def test_terminal_run_recovery_marks_only_dead_local_owner(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    ledger = RunLedger(
        db, process_id=999_999_999, hostname=socket.gethostname(),
        process_is_alive=lambda _pid: False,
    )
    ledger.begin("interrupted-run", kind="conversation")
    ledger.close()

    assert cli.main(["runs", "recover"]) == 0
    assert "Recovered 1 interrupted local run" in capsys.readouterr().out
    ledger = RunLedger(db)
    run = ledger.get("interrupted-run")
    ledger.close()
    assert run is not None
    assert run["state"] == "cancelled"
    assert "process ended before run completion" in run["error"]


def test_terminal_run_show_includes_child_and_correlated_task(tmp_path, monkeypatch, capsys):
    _operator_env(monkeypatch, tmp_path)
    db = tmp_path / "state.sqlite"
    ledger = RunLedger(db)
    ledger.begin("parent-run", kind="conversation", session_id="operator")
    ledger.record_event(Event(
        EventType.SUBAGENT_STARTED,
        {"run_id": "parent-run", "subagent_run_id": "child-run"},
    ))
    ledger.finish("child-run", state="ok")
    ledger.finish("parent-run", state="error", error="model failure")
    ledger.close()
    board = TaskBoard(db)
    task_id = board.enqueue("tool", run_id="parent-run")
    assert board.claim(task_id)
    board.fail(task_id, "dispatch failed")
    board.close()

    assert cli.main(["runs", "show", "parent-run"]) == 0
    output = capsys.readouterr().out
    assert "Child runs" in output and "child-run" in output
    assert "Correlated tasks" in output and f"[{task_id}]" in output
    assert "dispatch failed" in output


def test_m5_approval_handler_is_awaitable_for_existing_cli_dispatch_contract(tmp_path, monkeypatch):
    _operator_env(monkeypatch, tmp_path)
    with patch.object(cli, "_gateway_request", return_value={"pending": [], "pending_edits": 0}):
        assert asyncio.run(cli._approvals()) == 0
