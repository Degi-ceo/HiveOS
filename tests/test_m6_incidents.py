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
            evidence={"run_id": "diagnosis-run", "branches": ["hive/repair"],
                      "pr_url": "https://github.com/Degi-ceo/HiveOS/pull/123"},
        )
        links = ledger.links(incident["incident_id"])
        assert links is not None and links["status"] == "awaiting_review"
        assert any(link.get("run_id") == "diagnosis-run" for link in links["links"])
        assert any(link.get("pr_url", "").endswith("/123") for link in links["links"])
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
            branch="", approval_id="approval-123",
        )
        monkeypatch.setattr(HiveOS, "self_improve_from_symptom", AsyncMock(return_value=[outcome]))
        import asyncio
        result = asyncio.run(hive.diagnose_incident(incident["incident_id"]))
        assert result["status"] == "awaiting_review"
        assert hive.run_ledger.get(result["run_id"])["state"] == "ok"
        assert hive.incident_ledger.get(incident["incident_id"])["status"] == "awaiting_review"
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
