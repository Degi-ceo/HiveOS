"""Process handoff for an approved gateway restart remains durable and scoped."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.core.deployment_ledger import DEGRADED, HEALTHY, PENDING, DeployLedger
from hive.core.deployment_verifier import DeploymentVerifier, LocalGatewayHealth
from hive.core.service_identity import is_managed_gateway_process
from hive.core.types import ToolResult
from hive.gateway.app import create_app
from hive.runtime import HiveOS
from hive.tools.builtins import Deploy

SHA = "a" * 40
OLD_PROCESS = "b" * 32
NEW_PROCESS = "c" * 32


def _staged(ledger, *, host="host-a", now=100, scope="system"):
    return ledger.schedule(
        "run-29", "gateway", "systemctl", host, SHA,
        now=now, settling_seconds=0, await_restart=True,
        baseline_process_id=OLD_PROCESS, exclusive=True,
        systemctl_scope=scope,
    )


def test_live_receipt_refuses_parallel_restart_and_survives_reopen(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    first = _staged(ledger)
    with pytest.raises(ValueError, match="active"):
        _staged(DeployLedger(path), now=101)
    assert ledger.claim_due("host-a", "worker", now=101) is None
    confirmed = DeployLedger(path).confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, settling_seconds=5, now=102,
    )
    assert confirmed.id == first.id
    assert confirmed.status == PENDING
    assert confirmed.started_process_id == NEW_PROCESS
    assert confirmed.due_at == 160
    assert ledger.claim_due("host-a", "worker", now=159) is None
    assert ledger.claim_due("host-a", "worker", now=160).id == first.id


def test_same_process_wrong_revision_wrong_host_and_late_start_fail_closed(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _staged(ledger)
    for host, sha, process in (
        ("other-host", SHA, NEW_PROCESS),
        ("host-a", "d" * 40, NEW_PROCESS),
        ("host-a", SHA, OLD_PROCESS),
    ):
        assert ledger.confirm_gateway_start(
            host, sha, process, settling_seconds=0, now=101,
        ) is None
    assert ledger.get(item.id).restart_confirmed_at is None
    assert ledger.confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, settling_seconds=0, now=160,
    ) is None
    assert ledger.get(item.id).status == DEGRADED
    assert ledger.get(item.id).failed_signals == ("restart",)


def test_scope_change_cannot_confirm_staged_receipt_after_reopen(tmp_path):
    path = tmp_path / "deploy.sqlite"
    staged = _staged(DeployLedger(path), scope="user")
    assert DeployLedger(path).confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, systemctl_scope="system", now=101,
    ) is None
    assert DeployLedger(path).get(staged.id).restart_confirmed_at is None
    accepted = DeployLedger(path).confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, systemctl_scope="user", now=102,
    )
    assert accepted.id == staged.id
    assert accepted.systemctl_scope == "user"


def test_verifier_rejects_old_http_process_after_new_start(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _staged(ledger)
    confirmed = ledger.confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, settling_seconds=0, now=101,
    )
    assert confirmed.id == item.id
    health = LocalGatewayHealth("http://127.0.0.1:12345/health")
    data = {"status": "ok", "service": "hiveos-gateway",
            "source_revision": SHA, "process_instance_id": OLD_PROCESS}
    monkeypatch.setattr(health, "_fetch", lambda: data)
    assert asyncio.run(health.gateway(confirmed)) is False
    data = {**data, "process_instance_id": NEW_PROCESS}
    assert asyncio.run(health.gateway(confirmed)) is True
    assert asyncio.run(health.revision(confirmed)) == SHA


def test_baseline_health_requires_valid_pid_and_token(monkeypatch):
    health = LocalGatewayHealth("http://127.0.0.1:12345/health")
    data = {"status": "ok", "service": "hiveos-gateway",
            "process_instance_id": OLD_PROCESS, "process_pid": 123}
    monkeypatch.setattr(health, "_fetch", lambda: data)
    assert asyncio.run(health.current_process_identity()) == (OLD_PROCESS, 123)
    for changed in ({"process_pid": True}, {"process_pid": 0},
                    {"process_instance_id": "bad"}, {"status": "down"}):
        data.update(changed)
        assert asyncio.run(health.current_process_identity()) is None
        data = {"status": "ok", "service": "hiveos-gateway",
                "process_instance_id": OLD_PROCESS, "process_pid": 123}


def test_deploy_refuses_unobserved_baseline_without_restart(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    monkeypatch.setattr("hive.core.revision.detect_source_revision", lambda _root: SHA)
    commands = []

    class MissingHealth:
        async def current_process_identity(self):
            return None

    async def restart(_self, cmd, timeout=30):
        commands.append(cmd)
        return ToolResult(tool_name="deploy", success=True, content="ok")

    monkeypatch.setattr(Deploy, "_run_cmd", restart)
    tool = Deploy(deploy_ledger=ledger, host_key="host-a", repo_root=tmp_path,
                  settling_seconds=0, gateway_health=MissingHealth())
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is False
    assert "baseline gateway unavailable" in result.content
    assert commands == []


@pytest.mark.parametrize("scope", ["user", "system"])
def test_deploy_restarts_only_the_scope_owning_the_baseline(tmp_path, monkeypatch, scope):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    monkeypatch.setattr("hive.core.revision.detect_source_revision", lambda _root: SHA)
    probes = []
    commands = []

    class GatewayHealth:
        async def current_process_identity(self):
            return OLD_PROCESS, 123

    def managed(**kwargs):
        probes.append(kwargs)
        return kwargs["scope"] == scope

    async def restart(_self, cmd, timeout=30):
        commands.append(cmd)
        return ToolResult(tool_name="deploy", success=True, content="ok")

    monkeypatch.setattr("hive.core.service_identity.is_managed_gateway_process", managed)
    monkeypatch.setattr(Deploy, "_run_cmd", restart)
    tool = Deploy(deploy_ledger=ledger, host_key="host-a", repo_root=tmp_path,
                  gateway_health=GatewayHealth(), systemctl_scope=scope)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is True
    assert probes == [{"pid": 123, "scope": scope}]
    assert commands == [("systemctl", f"--{scope}", "restart", "hiveos-gateway.service")]

    probes.clear()
    commands.clear()
    blocked = Deploy(deploy_ledger=ledger, host_key="host-b", repo_root=tmp_path,
                     gateway_health=GatewayHealth(),
                     systemctl_scope="user" if scope == "system" else "system")
    refused = asyncio.run(blocked.execute(target="gateway", mode="systemctl"))
    assert refused.success is False
    assert "service identity mismatch" in refused.content
    assert commands == []


def test_unverified_local_systemctl_uses_configured_user_scope(tmp_path, monkeypatch):
    commands = []

    async def restart(_self, cmd, timeout=30):
        commands.append(cmd)
        return ToolResult(tool_name="deploy", success=True, content="ok")

    monkeypatch.setattr(Deploy, "_run_cmd", restart)
    result = asyncio.run(Deploy(systemctl_scope="user").execute(
        target="keeper", mode="systemctl",
    ))
    assert result.success is True
    assert commands == [("systemctl", "--user", "restart", "hiveos-keeper.service")]


def test_new_gateway_lifespan_confirms_receipt_after_caller_dies(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
        deploy_verify_settling_sec=0,
    )
    hive = HiveOS.build(cfg)
    ledger = hive.tools["deploy"]._deploy_ledger
    host_key = hive.tools["deploy"]._deploy_host_key
    receipt = _staged(ledger, host=host_key, now=ledger._clock())
    with patch("hive.gateway.app.detect_source_revision", return_value=SHA):
        with patch("hive.gateway.app._process_instance_id", return_value=NEW_PROCESS):
            with patch("hive.gateway.app.is_managed_gateway_process", return_value=True):
                app = create_app(hive)
                with TestClient(app) as client:
                    assert client.get("/health").json()["process_instance_id"] == NEW_PROCESS
                    confirmed = ledger.get(receipt.id)
                    assert confirmed.restart_confirmed_at is not None
                    assert confirmed.started_process_id == NEW_PROCESS
    assert DeployLedger(cfg.state_db).get(receipt.id).status == PENDING


def test_wrong_gateway_revision_does_not_confirm_on_startup(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
    )
    hive = HiveOS.build(cfg)
    ledger = hive.tools["deploy"]._deploy_ledger
    host_key = hive.tools["deploy"]._deploy_host_key
    receipt = _staged(ledger, host=host_key, now=ledger._clock())
    with patch("hive.gateway.app.detect_source_revision", return_value="d" * 40):
        with patch("hive.gateway.app._process_instance_id", return_value=NEW_PROCESS):
            with patch("hive.gateway.app.is_managed_gateway_process", return_value=True):
                app = create_app(hive)
                with TestClient(app):
                    assert ledger.get(receipt.id).restart_confirmed_at is None


def test_health_process_identity_is_resolved_after_app_creation(tmp_path):
    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False))
    with patch("hive.gateway.app._process_instance_id", return_value=NEW_PROCESS) as token:
        app = create_app(hive)
        assert token.call_count == 0
        with TestClient(app) as client:
            health = client.get("/health").json()
            assert health["process_instance_id"] == NEW_PROCESS
            assert health["process_pid"] > 0
        assert token.call_count == 1


def test_real_local_http_must_match_handed_off_process(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - HTTP handler API
            body = json.dumps({
                "status": "ok", "service": "hiveos-gateway",
                "source_revision": SHA, "process_instance_id": NEW_PROCESS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    clock = [100.0]
    ledger = DeployLedger(tmp_path / "deploy.sqlite", clock=lambda: clock[0])
    receipt = _staged(ledger)
    ledger.confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, now=101, settling_seconds=5,
    )
    clock[0] = 160.0

    async def healthy(_record):
        return True

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = LocalGatewayHealth(f"http://127.0.0.1:{server.server_port}/health")
            verifier = DeploymentVerifier(
                ledger, host_key="host-a", owner="worker",
                doctor=healthy, gateway=health.gateway, smoke=healthy,
                revision=health.revision,
            )
            verdict = asyncio.run(verifier.verify_due())
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert verdict.id == receipt.id
    assert verdict.status == HEALTHY


def test_unmanaged_competing_gateway_cannot_claim_staged_receipt(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
    )
    hive = HiveOS.build(cfg)
    ledger = hive.tools["deploy"]._deploy_ledger
    receipt = _staged(ledger, host=hive.tools["deploy"]._deploy_host_key,
                      now=ledger._clock())
    with patch("hive.gateway.app.detect_source_revision", return_value=SHA):
        with patch("hive.gateway.app._process_instance_id", return_value=NEW_PROCESS):
            with patch("hive.gateway.app.is_managed_gateway_process", return_value=False):
                with TestClient(create_app(hive)) as client:
                    assert client.get("/health").status_code == 200
                    assert ledger.get(receipt.id).restart_confirmed_at is None


def test_systemd_probe_matches_only_exact_main_pid_without_credentials(monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "test-privileged-secret")
    commands = []

    def run(cmd, **kwargs):
        commands.append((cmd, kwargs["env"]))
        value = "123\n" if cmd[1] == "--user" else "999\n"
        return subprocess.CompletedProcess(cmd, 0, value, "")

    assert is_managed_gateway_process(pid=123, scope="user", platform="linux", runner=run) is True
    assert len(commands) == 1
    assert commands[0][0][1] == "--user"
    assert commands[0][0][-1] == "hiveos-gateway.service"
    assert "HIVE_APPROVER_KEY" not in commands[0][1]
    assert is_managed_gateway_process(pid=999, scope="system", platform="linux", runner=run) is True
    assert is_managed_gateway_process(pid=456, platform="linux", runner=run) is False
    assert is_managed_gateway_process(pid=123, platform="win32", runner=run) is False
    assert is_managed_gateway_process(pid=123, scope="invalid", platform="linux", runner=run) is False


def test_pre_m29_inflight_receipt_migrates_to_degraded(tmp_path):
    path = tmp_path / "deploy.sqlite"
    old = DeployLedger(path)
    item = old.schedule("run-old", "gateway", "systemctl", "host-a", SHA,
                        now=100, settling_seconds=5)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE deploy_ledger DROP COLUMN started_process_id")
        db.execute("ALTER TABLE deploy_ledger DROP COLUMN baseline_process_id")
    migrated = DeployLedger(path)
    record = migrated.get(item.id)
    assert record.status == DEGRADED
    assert record.failed_signals == ("verifier",)
    assert migrated.next_degraded_without_incident("host-a").id == item.id


def test_receipt_without_persisted_scope_migrates_to_degraded(tmp_path):
    path = tmp_path / "deploy.sqlite"
    old = DeployLedger(path)
    item = _staged(old)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE deploy_ledger DROP COLUMN systemctl_scope")
    migrated = DeployLedger(path)
    assert migrated.get(item.id).status == DEGRADED
    assert migrated.get(item.id).failed_signals == ("verifier",)


def test_startup_cannot_be_healthy_before_bounded_command_can_fail(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _staged(ledger)
    confirmed = ledger.confirm_gateway_start(
        "host-a", SHA, NEW_PROCESS, settling_seconds=0, now=101,
    )
    assert confirmed.due_at == 160
    assert ledger.claim_due("host-a", "worker", now=130) is None
    failed = ledger.mark_restart_failed(item.id, now=131)
    assert failed.status == DEGRADED
    assert ledger.claim_due("host-a", "worker", now=160) is None


def test_late_restart_failure_revokes_only_live_healthy_receipt(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _staged(ledger)
    ledger.confirm_gateway_start("host-a", SHA, NEW_PROCESS,
                                 settling_seconds=0, now=101)
    assert ledger.claim_due("host-a", "worker", now=160).id == item.id
    assert ledger.finish(item.id, "worker", HEALTHY, now=161, claim_count=1).status == HEALTHY
    failed = ledger.mark_restart_failed(item.id, now=162)
    assert failed.status == DEGRADED
    assert failed.failed_signals == ("restart",)
