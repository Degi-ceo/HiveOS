"""Run views retain safe incident references after incident deduplication."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.observability.incidents import IncidentLedger
from hive.observability.runs import RunLedger
from hive.runtime import HiveOS
from hive.surfaces import cli


class _UnusedRouter:
    async def aclose(self) -> None:
        return None


def _failed_run(ledger: RunLedger, run_id: str) -> None:
    ledger.begin(run_id, kind="conversation", session_id="private-session")
    ledger.finish(run_id, state="error", error="provider:billing")


def _repeat(ledger: IncidentLedger, run_id: str) -> str:
    return ledger.record(
        "run", "provider:billing", run_id=run_id,
        occurrence_key=f"run:{run_id}:terminal",
    )["incident_id"]


def test_repeated_run_links_to_incident_after_restart_and_event_window(tmp_path):
    db = tmp_path / "state.sqlite"
    ledger = IncidentLedger(db)
    try:
        incident_id = _repeat(ledger, "first-run")
        assert _repeat(ledger, "second-run") == incident_id
        for index in range(205):
            _repeat(ledger, f"later-{index}")
        assert len(ledger.get(incident_id)["events"]) == 200
        assert ledger.for_run("second-run") == [{
            "incident_id": incident_id, "severity": "error", "status": "open",
        }]
        assert ledger.for_run("second") == []
        assert ledger.for_run("second-run' OR 1=1 --") == []
    finally:
        ledger.close()

    restarted = IncidentLedger(db)
    try:
        assert restarted.for_run("second-run")[0]["incident_id"] == incident_id
        assert restarted.for_run("first-run")[0]["incident_id"] == incident_id
    finally:
        restarted.close()


def test_legacy_primary_run_link_and_missing_run(tmp_path):
    ledger = IncidentLedger(tmp_path / "state.sqlite")
    try:
        incident = ledger.record("task", "safe failure", run_id="legacy-run")
        assert ledger.for_run("legacy-run")[0]["incident_id"] == incident["incident_id"]
        assert ledger.for_run("unrelated-run") == []
        assert ledger.for_run("") == []
    finally:
        ledger.close()


def test_report_and_run_show_link_to_repeated_incident(tmp_path, monkeypatch, capsys):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        state_db=tmp_path / "state.sqlite",
    )
    runs = RunLedger(cfg.state_db)
    incidents = IncidentLedger(cfg.state_db)
    try:
        _failed_run(runs, "first-run")
        _failed_run(runs, "second-run")
        incident_id = _repeat(incidents, "first-run")
        _repeat(incidents, "second-run")
    finally:
        runs.close()
        incidents.close()
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))

    assert cli.main(["report", "second-run"]) == 0
    report = capsys.readouterr().out
    assert incident_id in report
    assert f"hive incidents show {incident_id}" in report
    assert "private-session" not in report
    assert cli.main(["runs", "show", "second-run"]) == 0
    shown = capsys.readouterr().out
    assert incident_id in shown
    assert "private-session" not in shown


def test_gateway_run_snapshot_links_repeated_incident_without_control(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="agent-key", host="127.0.0.1", autonomy_enabled=False, production_mode=False,
    )
    hive = HiveOS.build(cfg, router=_UnusedRouter())
    try:
        _failed_run(hive.run_ledger, "first-run")
        _failed_run(hive.run_ledger, "second-run")
        incident_id = _repeat(hive.incident_ledger, "first-run")
        _repeat(hive.incident_ledger, "second-run")
        with TestClient(create_app(hive)) as client:
            assert client.get("/runs/second-run").status_code == 401
            response = client.get("/runs/second-run", headers={"X-Hive-Token": "agent-key"})
        assert response.status_code == 200
        assert response.json()["incidents"] == [{
            "incident_id": incident_id, "severity": "error", "status": "open",
        }]
        assert "private-session" not in response.text
    finally:
        asyncio.run(hive.aclose())


def test_gateway_run_snapshot_survives_incident_store_failure(tmp_path, monkeypatch, caplog):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="agent-key", host="127.0.0.1", autonomy_enabled=False, production_mode=False,
    )
    hive = HiveOS.build(cfg, router=_UnusedRouter())
    try:
        _failed_run(hive.run_ledger, "failed-run")

        def unavailable(_run_id):
            raise OSError("private storage detail")

        monkeypatch.setattr(hive.incident_ledger, "for_run", unavailable)
        with TestClient(create_app(hive)) as client:
            response = client.get("/runs/failed-run", headers={"X-Hive-Token": "agent-key"})
        assert response.status_code == 200
        assert response.json()["state"] == "error"
        assert response.json()["incidents"] == []
        assert response.json()["incident_lookup_available"] is False
        assert "private storage detail" not in response.text + caplog.text
    finally:
        asyncio.run(hive.aclose())


def test_terminal_failure_prints_deduplicated_incident_reference(tmp_path, monkeypatch, capsys):
    hive = HiveOS.build(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_UnusedRouter(),
    )

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "billing"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)
    try:
        assert asyncio.run(cli._terminal_turn(hive, "first", session_id="first")) == 1
        first_output = capsys.readouterr().out
        incident_id = hive.incident_ledger.recent()[0]["incident_id"]
        assert incident_id in first_output
        assert asyncio.run(cli._terminal_turn(hive, "second", session_id="second")) == 1
        second_output = capsys.readouterr().out
        assert incident_id in second_output
        assert f"hive incidents show {incident_id}" in second_output
    finally:
        asyncio.run(hive.aclose())


def test_terminal_lookup_failure_does_not_mask_provider_error(tmp_path, monkeypatch, capsys):
    hive = HiveOS.build(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_UnusedRouter(),
    )

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "billing"}

    def failed_lookup(_run_id):
        raise OSError("private incident database detail")

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)
    monkeypatch.setattr(hive.incident_ledger, "for_run", failed_lookup)
    try:
        assert asyncio.run(cli._terminal_turn(hive, "test", session_id="test")) == 1
        output = capsys.readouterr().out
        assert "billing" in output
        assert "lookup unavailable" in output
        assert "private incident database detail" not in output
    finally:
        asyncio.run(hive.aclose())


def test_gateway_incident_reference_renderer_rejects_untrusted_values(capsys):
    cli._render_run_incidents([{
        "incident_id": "\x1b[31mprivate-id", "status": "open",
    }, {
        "incident_id": "36db715b-b9eb-46bb-a165-f1cabd5d999c", "status": "secret\nvalue",
    }])
    output = capsys.readouterr().out
    assert "36db715b-b9eb-46bb-a165-f1cabd5d999c (unknown)" in output
    assert "private-id" not in output
    assert "secret" not in output
    assert "\x1b" not in output
