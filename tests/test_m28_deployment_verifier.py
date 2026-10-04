"""The verifier is bounded, host-scoped and stores no raw probe evidence."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger
from hive.core.deployment_verifier import (
    DeploymentVerifier, LocalGatewayHealth, deterministic_smoke_probe,
    local_doctor_probe,
)
from hive.core.config import HiveConfig
from hive.autonomy.heartbeat import Heartbeat
from hive.runtime import HiveOS

SHA = "a" * 40


def _verifier(ledger, *, host="local", owner="worker", doctor=None, gateway=None,
              smoke=None, revision=None, timeout=0.1):
    async def healthy(_record):
        return True

    async def current(_record):
        return SHA

    return DeploymentVerifier(
        ledger, host_key=host, owner=owner,
        doctor=doctor or healthy, gateway=gateway or healthy,
        smoke=smoke or healthy, revision=revision or current,
        probe_timeout_seconds=timeout,
    )


def _ledger(tmp_path, clock):
    ledger = DeployLedger(tmp_path / "deploy.sqlite", clock=lambda: clock[0])
    item = ledger.schedule("run-1", "gateway", "systemctl", "local", SHA,
                           now=clock[0], settling_seconds=5)
    return ledger, item


def test_healthy_verdict_waits_for_settling_and_survives_restart(tmp_path):
    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    assert asyncio.run(_verifier(ledger).verify_due()) is None
    clock[0] = 105.0
    result = asyncio.run(_verifier(ledger).verify_due())
    assert result.id == item.id
    assert result.verdict == HEALTHY
    assert result.failed_signals == ()
    assert DeployLedger(tmp_path / "deploy.sqlite").get(item.id).verdict == HEALTHY
    assert asyncio.run(_verifier(ledger).verify_due()) is None


def test_failure_codes_never_store_probe_exception_or_raw_output(tmp_path):
    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    clock[0] = 105.0

    async def failing(_record):
        raise RuntimeError("raw secret-bearing probe error")

    async def stale(_record):
        return "b" * 40

    result = asyncio.run(_verifier(ledger, doctor=failing, revision=stale).verify_due())
    assert result.verdict == DEGRADED
    assert result.failed_signals == ("doctor", "revision")
    assert b"raw secret-bearing probe error" not in (tmp_path / "deploy.sqlite").read_bytes()
    assert ledger.get(item.id) == result


def test_timeout_is_bounded_and_redacted(tmp_path):
    clock = [100.0]
    ledger, _ = _ledger(tmp_path, clock)
    clock[0] = 105.0

    async def slow(_record):
        await asyncio.sleep(1)
        return True

    result = asyncio.run(_verifier(ledger, smoke=slow, timeout=0.01).verify_due())
    assert result.verdict == DEGRADED
    assert result.failed_signals == ("smoke", "timeout")


def test_remote_host_is_not_claimed(tmp_path):
    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    clock[0] = 105.0
    assert asyncio.run(_verifier(ledger, host="remote").verify_due()) is None
    assert ledger.get(item.id).verdict is None


def test_concurrent_workers_get_one_receipt(tmp_path):
    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    clock[0] = 105.0

    async def run_both():
        return await asyncio.gather(
            _verifier(ledger, owner="one").verify_due(),
            _verifier(ledger, owner="two").verify_due(),
        )

    results = asyncio.run(run_both())
    assert sum(result is not None for result in results) == 1
    assert ledger.get(item.id).claim_count == 1


def test_cancellation_leaves_lease_for_one_bounded_recovery(tmp_path):
    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    clock[0] = 105.0

    async def cancelled(_record):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_verifier(ledger, doctor=cancelled).verify_due())
    first = ledger.get(item.id)
    assert first.claim_count == 1
    assert first.verdict is None
    assert asyncio.run(_verifier(ledger, owner="other").verify_due()) is None
    clock[0] = first.lease_until + 0.1
    result = asyncio.run(_verifier(ledger, owner="other").verify_due())
    assert result.verdict == HEALTHY
    assert result.claim_count == 2


@pytest.mark.parametrize("timeout", [0, -1, 601, float("inf"), float("nan"), True])
def test_invalid_timeout_is_rejected(tmp_path, timeout):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    with pytest.raises(ValueError):
        _verifier(ledger, timeout=timeout)


def test_local_gateway_probe_refuses_legacy_receipt_without_process_identity(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - HTTP handler API
            body = json.dumps({
                "status": "ok", "service": "hiveos-gateway",
                "source_revision": SHA,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    clock = [100.0]
    ledger, item = _ledger(tmp_path, clock)
    clock[0] = 105.0
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe = LocalGatewayHealth(f"http://127.0.0.1:{server.server_port}/health")
            result = asyncio.run(_verifier(
                ledger, gateway=probe.gateway, revision=probe.revision,
            ).verify_due())
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert result.id == item.id
    assert result.verdict == DEGRADED
    assert result.failed_signals == ("gateway",)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:1234/health", "http://localhost:1234/health",
    "http://127.0.0.1:1234/health?token=x", "http://127.0.0.1:1234/other",
    "http://user@127.0.0.1:1234/health", "http://example.com:1234/health",
])
def test_gateway_probe_rejects_nonlocal_or_ambiguous_url(url):
    with pytest.raises(ValueError):
        LocalGatewayHealth(url)


def test_doctor_probe_uses_real_doctor_contract_without_rendering_details(
    tmp_path, monkeypatch, capsys,
):
    from hive.core import doctor

    ledger, item = _ledger(tmp_path, [100.0])
    del ledger
    monkeypatch.setattr(doctor, "check", lambda fix=False: [
        ("data dir present", True, "raw-secret-detail"),
        ("mnemosyne home dir", False, "raw-secret-detail"),
    ])
    assert asyncio.run(local_doctor_probe(item)) is True
    assert "raw-secret-detail" not in capsys.readouterr().out


def test_deterministic_smoke_real_hive_process(tmp_path):
    ledger, item = _ledger(tmp_path, [100.0])
    del ledger
    root = Path(__file__).parents[1]
    assert asyncio.run(deterministic_smoke_probe(item, repo_root=root)) is True


def test_smoke_child_environment_omits_approver_key(tmp_path, monkeypatch):
    ledger, item = _ledger(tmp_path, [100.0])
    del ledger
    root = Path(__file__).parents[1]
    monkeypatch.setenv("HIVE_APPROVER_KEY", "test-only-privileged-key")
    captured = {}

    class FakeProcess:
        async def wait(self):
            return 0

    async def fake_spawn(*_args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    assert asyncio.run(deterministic_smoke_probe(item, repo_root=root)) is True
    assert not any(key.casefold() == "hive_approver_key" for key in captured)
    assert "PYTHONPATH" in captured
    assert "MINIMAX_API_KEY" not in captured


class _Router:
    async def aclose(self):
        return None


def test_runtime_wires_opt_in_verifier_and_keeps_default_off(tmp_path):
    normal = HiveOS.build(HiveConfig.from_env(root=tmp_path / "normal", load_dotenv=False),
                          router=_Router())
    assert normal.deploy_verifier is None
    assert normal.tools["deploy"]._deploy_ledger is None

    config = replace(
        HiveConfig.from_env(root=tmp_path / "enabled", load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver-key",
        worker_isolation="required", deploy_verify_enabled=True,
        deploy_verify_settling_sec=0,
    )
    hive = HiveOS.build(config, router=_Router())
    assert isinstance(hive.deploy_verifier, DeploymentVerifier)
    assert hive.tools["deploy"]._deploy_ledger is not None
    assert isinstance(hive.tools["deploy"]._gateway_health, LocalGatewayHealth)
    assert hive.tools["deploy"]._deploy_settling_seconds == 0


@pytest.mark.parametrize("changes,expected", [
    ({"deploy_verify_enabled": True}, "HIVE_DEPLOY_VERIFY_ENABLED"),
    ({"autonomy_enabled": True, "deploy_verify_enabled": True,
      "approver_key": ""}, "HIVE_APPROVER_KEY"),
    ({"deploy_verify_settling_sec": float("nan")}, "HIVE_DEPLOY_VERIFY_SETTLING_SEC"),
    ({"deploy_systemctl_scope": "invalid"}, "HIVE_DEPLOY_SYSTEMCTL_SCOPE"),
])
def test_runtime_rejects_unsafe_verification_config(tmp_path, changes, expected):
    config = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), **changes)
    with pytest.raises(RuntimeError, match=expected):
        HiveOS.build(config, router=_Router())


def test_heartbeat_verifies_even_when_budget_halts_work(tmp_path, monkeypatch):
    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver-key",
        worker_isolation="required", deploy_verify_enabled=True,
    )
    hive = HiveOS.build(config, router=_Router())
    ledger = hive.tools["deploy"]._deploy_ledger
    host_key = hive.tools["deploy"]._deploy_host_key
    receipt = ledger.schedule("run-141", "gateway", "systemctl", host_key, SHA,
                              settling_seconds=0)

    class FailingVerifier:
        async def verify_due(self):
            claimed = ledger.claim_due(host_key, "test-worker", lease_seconds=60)
            return ledger.finish(claimed.id, "test-worker", DEGRADED,
                                 failed_signals=("gateway",), claim_count=claimed.claim_count)

        def next_incident(self):
            return ledger.next_degraded_without_incident(host_key)

        def mark_incident_recorded(self, id):
            return ledger.mark_incident_recorded(id, host_key)

    hive.deploy_verifier = FailingVerifier()
    monkeypatch.setattr(hive.budgeter, "daily_spend_status", lambda: {
        "hard_cap_reached": True, "cost_usd": 1.0, "cap_usd": 1.0,
    })
    summary = asyncio.run(Heartbeat(hive).tick())
    assert summary["paused"] is True
    assert ledger.get(receipt.id).status == DEGRADED
    with hive.incident_ledger._lock:
        row = hive.incident_ledger._db.execute(
            "SELECT source, run_id FROM hive_incidents WHERE run_id=?", ("run-141",),
        ).fetchone()
    assert tuple(row) == ("deploy", "run-141")
    assert ledger.get(receipt.id).incident_recorded_at is not None


def test_expired_verification_is_reported_after_restart_without_duplicate_incident(
    tmp_path, monkeypatch,
):
    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver-key",
        worker_isolation="required", deploy_verify_enabled=True,
    )
    hive = HiveOS.build(config, router=_Router())
    ledger = hive.tools["deploy"]._deploy_ledger
    host_key = hive.tools["deploy"]._deploy_host_key
    receipt = ledger.schedule("run-expired", "gateway", "systemctl", host_key, SHA,
                              now=100, settling_seconds=0)
    ledger.claim_due(host_key, "old-worker", now=100, lease_seconds=1)
    ledger.claim_due(host_key, "retry-worker", now=101, lease_seconds=1)
    # A new verifier after restart finds the expired claim; no probe returns it.
    assert asyncio.run(hive.deploy_verifier.verify_due()) is None
    assert ledger.get(receipt.id).status == DEGRADED
    monkeypatch.setattr(hive.budgeter, "daily_spend_status", lambda: {
        "hard_cap_reached": True, "cost_usd": 1.0, "cap_usd": 1.0,
    })
    heartbeat = Heartbeat(hive)
    assert asyncio.run(heartbeat.tick())["paused"] is True
    assert ledger.get(receipt.id).incident_recorded_at is not None
    assert asyncio.run(heartbeat.tick())["paused"] is True
    with hive.incident_ledger._lock:
        rows = hive.incident_ledger._db.execute(
            "SELECT incident_id FROM hive_incidents WHERE run_id=?", ("run-expired",),
        ).fetchall()
        events = hive.incident_ledger._db.execute(
            "SELECT id FROM hive_incident_events WHERE occurrence_key=?",
            (f"deploy:{receipt.id}",),
        ).fetchall()
    assert len(rows) == len(events) == 1


def test_failed_restart_receipt_is_replayed_into_incident_after_restart(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    receipt = ledger.schedule("run-restart", "gateway", "systemctl", "host-a", SHA,
                              settling_seconds=0)
    ledger.mark_restart_failed(receipt.id)
    reopened = DeployLedger(path)
    queued = reopened.next_degraded_without_incident("host-a")
    assert queued.id == receipt.id
    assert queued.failed_signals == ("restart",)
    assert reopened.next_degraded_without_incident("other-host") is None
    with pytest.raises(ValueError):
        reopened.mark_incident_recorded(receipt.id, "other-host")
    first = reopened.mark_incident_recorded(receipt.id, "host-a")
    assert first.incident_recorded_at is not None
    assert reopened.mark_incident_recorded(receipt.id, "host-a") == first
    assert reopened.next_degraded_without_incident("host-a") is None
