"""Operator status JSON is a read-only, redacted CLI contract."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

from hive.core.config import HiveConfig
from hive.observability.runs import RunLedger
from hive.surfaces import cli
from hive.surfaces.cli import parser


def _config(tmp_path, monkeypatch, **changes):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        state_db=tmp_path / "state.sqlite",
        secret="private-agent-credential",
        approver_key="private-approver-credential",
        **changes,
    )
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))
    return cfg


def _invoke(capsys, *args):
    rc = cli.main(["status", *args])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert "\x1b[" not in captured.out
    assert "private-agent-credential" not in captured.out
    assert "private-approver-credential" not in captured.out
    return rc, json.loads(captured.out)


def test_status_json_missing_database_is_read_only(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    rc, payload = _invoke(capsys, "--json")
    assert payload["schema_version"] == 1
    assert payload["state_db_exists"] is False
    assert payload["dead_tasks"] is None
    assert payload["executions"] is None
    assert payload["ok"] is (rc == 0)
    assert not cfg.state_db.exists()


def test_status_json_missing_database_fails_requested_live_read(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    rc, payload = _invoke(capsys, "--json", "--live")
    assert rc == 1
    assert payload["ok"] is False
    assert payload["executions"] is None
    assert payload["execution_error"] == "run_ledger_unavailable"
    assert not cfg.state_db.exists()


def test_status_json_counts_dead_tasks_and_recent_runs(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    ledger = RunLedger(cfg.state_db)
    try:
        ledger.begin("run-one", kind="conversation", session_id="private-chat-id")
        ledger.finish("run-one", state="error", error="secret-bearing error")
        ledger.begin("run-two", kind="conversation", session_id="private-chat-id")
    finally:
        ledger.close()
    with sqlite3.connect(cfg.state_db) as conn:
        conn.execute("CREATE TABLE hive_tasks(state TEXT)")
        conn.executemany("INSERT INTO hive_tasks(state) VALUES (?)", [("dead",), ("done",)])

    rc, payload = _invoke(capsys, "--live", "--json")
    assert payload["ok"] is (rc == 0)
    assert payload["dead_tasks"] == 1
    assert payload["executions"] == {"running": 1, "ok": 0, "error": 1, "cancelled": 0}
    assert payload["execution_source"] == "local"
    assert payload["execution_error"] is None
    assert "private-chat-id" not in json.dumps(payload)
    assert "secret-bearing error" not in json.dumps(payload)


def test_status_json_handles_missing_tables_without_raw_sql_error(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    with sqlite3.connect(cfg.state_db):
        pass
    rc, payload = _invoke(capsys, "--json", "--live")
    assert rc == 1
    assert payload["dead_tasks_available"] is False
    assert payload["executions"] is None
    assert payload["execution_error"] == "run_ledger_unavailable"


def test_status_json_redacts_config_warnings(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    monkeypatch.setattr(HiveConfig, "validate", lambda self: ["secret-bearing warning private-agent-credential"])
    rc, payload = _invoke(capsys, "--json")
    assert rc == 1
    assert payload["config_warning_count"] == 1
    assert payload["config_ok"] is False
    assert "secret-bearing warning" not in json.dumps(payload)


def test_status_json_gateway_accepts_only_safe_counts(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch, host="127.0.0.1")
    monkeypatch.setattr(
        cli, "_gateway_request",
        lambda _cfg, _method, _path, **_kwargs: {
            "executions": {"running": 2, "ok": 3, "error": 0, "cancelled": 1},
            "raw_secret": "do-not-render",
        },
    )
    rc, payload = _invoke(capsys, "--json", "--gateway", "--live")
    assert payload["ok"] is (rc == 0)
    assert payload["execution_source"] == "gateway"
    assert payload["executions"]["running"] == 2
    assert "do-not-render" not in json.dumps(payload)


def test_status_json_gateway_failure_is_one_json_object(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch, host="remote.example")
    rc, payload = _invoke(capsys, "--json", "--live", "--gateway")
    assert rc == 1
    assert payload["execution_error"] == "gateway_unavailable"
    assert payload["executions"] is None


def test_status_json_gateway_http_error_does_not_add_text(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch, host="127.0.0.1")

    def reject(_request, timeout):
        assert timeout == 5
        raise HTTPError("http://127.0.0.1", 401, "raw private error", {}, None)

    monkeypatch.setattr(cli.urllib.request, "urlopen", reject)
    rc, payload = _invoke(capsys, "--json", "--live", "--gateway")
    assert rc == 1
    assert payload["execution_error"] == "gateway_unavailable"


def test_status_json_rejects_invalid_flags_without_text(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    for flags in (("--json", "--gateway"), ("--json", "--bad"), ("--json", "--json")):
        rc, payload = _invoke(capsys, *flags)
        assert rc == 2
        assert payload == {"ok": False, "error": "invalid_arguments"}


def test_status_parser_recognizes_boolean_flags():
    _spec, options = parser.parse(["status", "--json", "--live", "--gateway"])
    assert options.json is True
    assert options.live is True
    assert options.gateway is True


def test_real_terminal_status_json_is_single_redacted_line(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "PYTHONPATH": str(repo / "src"),
        "HIVE_STATE_DB": str(tmp_path / "missing.sqlite"),
        "HIVE_ENV_FILE": str(tmp_path / ".env"),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-m", "hive.surfaces.cli", "status", "--json"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode in {0, 1}
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is (result.returncode == 0)
    assert payload["state_db_exists"] is False
    assert not (tmp_path / "missing.sqlite").exists()


def test_real_terminal_invalid_config_remains_redacted_json(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "PYTHONPATH": str(repo / "src"),
        "HIVE_ENV_FILE": str(tmp_path / ".env"),
        "HIVE_PORT": "private-malformed-value",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-m", "hive.surfaces.cli", "status", "--json"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1, "ok": False, "error": "config_unavailable",
    }
    assert "private-malformed-value" not in result.stdout
