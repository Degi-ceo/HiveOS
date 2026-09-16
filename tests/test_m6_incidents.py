"""M6 incident lifecycle: durable redaction, bounded recovery, operator auth."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.testclient import TestClient
import pytest

from hive.autonomy.tasks import FAILED, PENDING
from hive.core.config import HiveConfig
from hive.core.spec_search import EditOp, RiskTier
from hive.gateway.app import create_app
from hive.observability.incidents import IncidentLedger
from hive.runtime import HiveOS
from hive.surfaces import cli


class _Router:
    async def complete(self, *_args, **_kwargs):
        raise RuntimeError("synthetic provider failure token=secret-value")

    async def aclose(self):
        pass


def _hive(tmp_path, monkeypatch) -> HiveOS:
    monkeypatch.setenv("HIVE_SECRET", "agent-key")
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-key")
    monkeypatch.setenv("HIVE_AUTONOMY_ENABLED", "true")
    monkeypatch.setenv("HIVE_WORKER_ISOLATION", "required")
    monkeypatch.setenv("HIVE_AUDIT_INTEGRITY_KEY", "audit-integrity-key")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_Router())


def test_ledger_deduplicates_redacts_and_survives_restart(tmp_path):
    db = tmp_path / "state.sqlite"
    first = IncidentLedger(db)
    incident = first.record("task", "failure token=secret-value", evidence={"token": "secret-value"})
    repeated = first.record("task", "failure token=secret-value")
    assert incident["incident_id"] == repeated["incident_id"]
    assert "secret-value" not in incident["summary"]
    first.close()

    second = IncidentLedger(db)
    restored = second.get(incident["incident_id"])
    assert restored is not None
    assert len(restored["events"]) == 2
    assert "secret-value" not in str(restored)
    second.close()


def test_ledger_concurrent_records_share_one_active_incident(tmp_path):
    ledger = IncidentLedger(tmp_path / "state.sqlite")
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: ledger.record("task", "same failure"), range(12)))
        assert len({record["incident_id"] for record in records}) == 1
        assert len(ledger.recent()) == 1
    finally:
        ledger.close()


def test_diagnosis_records_safe_branch_and_pr_links(tmp_path):
    ledger = IncidentLedger(tmp_path / "state.sqlite")
    try:
        incident = ledger.record("task", "repeated timeout")
        assert ledger.begin_diagnosis(incident["incident_id"], run_id="diagnosis-run")
        assert ledger.record_remediation(
            incident["incident_id"], status="awaiting_review",
            evidence={"run_id": "diagnosis-run", "remediation_refs": [{
                "branch": "hive/repair", "pr_url": "https://github.com/Degi-ceo/HiveOS/pull/123",
            }]},
        )
        links = ledger.links(incident["incident_id"])
        assert links is not None and links["status"] == "awaiting_review"
        assert any(link.get("run_id") == "diagnosis-run" for link in links["links"])
        assert any(link.get("pr_url", "").endswith("/123") for link in links["links"])
    finally:
        ledger.close()


def test_ledger_recovery_claim_is_atomic_across_connections(tmp_path):
    db = tmp_path / "state.sqlite"
    first = IncidentLedger(db)
    second = IncidentLedger(db)
    try:
        incident = first.record("task", "same recoverable failure")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(
                lambda ledger: ledger.begin_recovery(incident["incident_id"], cooldown_seconds=0),
                (first, second),
            ))
        assert sum(claim is not None for claim in claims) == 1
        current = first.get(incident["incident_id"])
        assert current is not None and current["recovery_count"] == 1
        assert sum(event["type"] == "recovery_started" for event in current["events"]) == 1
    finally:
        first.close()
        second.close()


def test_ledger_returns_newest_events_in_chronological_order(tmp_path):
    ledger = IncidentLedger(tmp_path / "state.sqlite")
    try:
        incident = ledger.record("task", "noisy failure")
        for _ in range(205):
            ledger.record("task", "noisy failure")
        assert ledger.acknowledge(incident["incident_id"])
        restored = ledger.get(incident["incident_id"])
        assert restored is not None and len(restored["events"]) == 200
        assert restored["events"][-1]["type"] == "acknowledged"
        assert [event["id"] for event in restored["events"]] == sorted(event["id"] for event in restored["events"])
    finally:
        ledger.close()


def test_runtime_reconciles_failed_task_and_recovery_is_bounded(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    try:
        task_id = hive.task_board.enqueue("diagnostic")
        assert hive.task_board.claim(task_id)
        hive.task_board.fail(task_id, "temporary failure token=secret-value")
        assert hive.task_board.get(task_id).state == FAILED
        assert hive.reconcile_incidents() >= 1
        incident = next(item for item in hive.incident_ledger.recent() if item["task_id"] == task_id)
        outcome = hive.recover_incident(incident["incident_id"])
        assert outcome["recovered"] is True
        assert hive.task_board.get(task_id).state == PENDING
        assert hive.incident_ledger.get(incident["incident_id"])["status"] == "resolved"
        with pytest.raises(ValueError, match="not eligible"):
            hive.recover_incident(incident["incident_id"])
    finally:
        import asyncio
        asyncio.run(hive.aclose())


def test_gateway_incident_mutations_require_approver_key(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    try:
        incident = hive.incident_ledger.record("run", "failed diagnostic")
        with TestClient(create_app(hive)) as client:
            assert client.get("/incidents", headers={"X-Hive-Token": "agent-key"}).status_code == 200
            path = f"/incidents/{incident['incident_id']}/acknowledge"
            assert client.post(path, json={}, headers={"X-Hive-Token": "agent-key"}).status_code == 401
            assert client.post(path, json={}, headers={"X-Hive-Token": "approver-key"}).status_code == 200
    finally:
        import asyncio
        asyncio.run(hive.aclose())


def test_gateway_incident_diagnosis_requires_approver_key(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    try:
        incident = hive.incident_ledger.record("run", "failed diagnostic")
        diagnose = AsyncMock(return_value={"status": "awaiting_review"})
        monkeypatch.setattr(HiveOS, "diagnose_incident", diagnose)
        with TestClient(create_app(hive)) as client:
            path = f"/incidents/{incident['incident_id']}/diagnose"
            assert client.post(path, json={}, headers={"X-Hive-Token": "agent-key"}).status_code == 401
            assert client.post(path, json={}, headers={"X-Hive-Token": "approver-key"}).status_code == 200
        diagnose.assert_awaited_once_with(incident["incident_id"])
    finally:
        import asyncio
        asyncio.run(hive.aclose())


def test_runtime_diagnosis_creates_correlated_run_and_review_state(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    try:
        incident = hive.incident_ledger.record("task", "repeated timeout")
        outcome = SimpleNamespace(
            op=EditOp.CREATE_FILE, tier=RiskTier.REVIEW, status="pending_approval",
            branch="hive/incident-repair", approval_id="approval-123",
        )
        monkeypatch.setattr(HiveOS, "self_improve_from_symptom", AsyncMock(return_value=[outcome]))
        monkeypatch.setattr(hive.self_modifier, "history", lambda **_kwargs: [{
            "branch": "hive/incident-repair", "pr_url": "https://github.com/Degi-ceo/HiveOS/pull/456",
        }])
        import asyncio
        result = asyncio.run(hive.diagnose_incident(incident["incident_id"]))
        assert result["status"] == "awaiting_review"
        assert hive.run_ledger.get(result["run_id"])["state"] == "ok"
        assert hive.incident_ledger.get(incident["incident_id"])["status"] == "awaiting_review"
        links = hive.incident_links(incident["incident_id"])["links"]
        assert any(link.get("branch") == "hive/incident-repair" for link in links)
        assert any(link.get("pr_url", "").endswith("/456") for link in links)
    finally:
        import asyncio
        asyncio.run(hive.aclose())


def test_runtime_diagnosis_reports_acknowledgement_that_wins_finalization(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    try:
        incident = hive.incident_ledger.record("task", "concurrent diagnosis")
        outcome = SimpleNamespace(
            op=EditOp.CREATE_FILE, tier=RiskTier.REVIEW, status="pending_approval",
            branch="hive/incident-race", approval_id="approval-race",
        )
        monkeypatch.setattr(HiveOS, "self_improve_from_symptom", AsyncMock(return_value=[outcome]))
        original = hive.incident_ledger.record_remediation

        def _acknowledge_before_remediation(incident_id, **kwargs):
            assert hive.incident_ledger.acknowledge(incident_id)
            return original(incident_id, **kwargs)

        monkeypatch.setattr(hive.incident_ledger, "record_remediation", _acknowledge_before_remediation)
        import asyncio
        result = asyncio.run(hive.diagnose_incident(incident["incident_id"]))
        assert result["finalized"] is False
        assert result["status"] == "suppressed"
        assert not any("branch" in link for link in hive.incident_links(incident["incident_id"])["links"])
    finally:
        import asyncio
        asyncio.run(hive.aclose())


def test_terminal_incident_recovery_uses_approver_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HIVE_SECRET", "agent-key")
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-key")
    monkeypatch.setenv("HIVE_HOST", "127.0.0.1")
    monkeypatch.setenv("HIVE_PORT", "18088")
    captured = {}

    def _request(cfg, method, path, *, credential, body=None, approver=False):
        captured.update(method=method, path=path, credential=credential, body=body, approver=approver)
        return {"recovered": True}

    monkeypatch.setattr(cli, "_gateway_request", _request)
    assert cli.main(["incidents", "recover", "incident-123"]) == 0
    assert captured == {
        "method": "POST", "path": "/incidents/incident-123/recover",
        "credential": "approver-key", "body": {}, "approver": True,
    }
    assert "approver-key" not in capsys.readouterr().out


def test_terminal_incident_recovery_reports_unfinalized_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HIVE_SECRET", "agent-key")
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-key")
    monkeypatch.setattr(
        cli, "_gateway_request", lambda *_args, **_kwargs: {"recovered": True, "finalized": False},
    )
    assert cli.main(["incidents", "recover", "incident-123"]) == 1
    output = capsys.readouterr().out
    assert "superseded" in output
    assert "approver-key" not in output


def test_terminal_incident_diagnosis_uses_approver_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET", "agent-key")
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-key")
    captured = {}

    def _request(cfg, method, path, *, credential, body=None, approver=False):
        captured.update(method=method, path=path, credential=credential, approver=approver)
        return {"status": "awaiting_review"}

    monkeypatch.setattr(cli, "_gateway_request", _request)
    assert cli.main(["incidents", "diagnose", "incident-123"]) == 0
    assert captured == {
        "method": "POST", "path": "/incidents/incident-123/diagnose",
        "credential": "approver-key", "approver": True,
    }
