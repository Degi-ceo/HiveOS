"""M37: one-shot, same-revision deployment recovery remains fail-closed."""

from __future__ import annotations

import asyncio
import hashlib
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hive.autonomy.heartbeat import Heartbeat
from hive.core.config import HiveConfig
from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger
from hive.core.deployment_recovery import (
    AWAITING_VERDICT,
    FAILED,
    UNCERTAIN,
    DeploymentRecoveryController,
    DeploymentRecoveryLedger,
)
from hive.core.safety_state import SafetyStateStore
from hive.core.deployment_recovery_runner import restart_gateway_systemctl
from hive.core.service_identity import is_managed_gateway_process
from hive.runtime import HiveOS

SHA = "a" * 40
OLD_PROCESS = "b" * 32
NEW_PROCESS = "c" * 32


class _Health:
    async def current_process_identity(self):
        return NEW_PROCESS, 123

    async def revision(self, _record):
        return SHA


def _degraded(ledger: DeployLedger, *, host: str = "host-a"):
    receipt = ledger.schedule(
        "run-37", "gateway", "systemctl", host, SHA, baseline_sha=SHA,
        now=100, await_restart=True, baseline_process_id=OLD_PROCESS,
        exclusive=True,
    )
    ledger.confirm_gateway_start(host, SHA, NEW_PROCESS, settling_seconds=0, now=101)
    return ledger.mark_restart_failed(receipt.id, now=102)


def _controller(tmp_path, ledger, restart):
    return DeploymentRecoveryController(
        ledger, state_db=tmp_path / "state.sqlite", host_key="host-a",
        repo_root=tmp_path, gateway_health=_Health(), systemctl_scope="system",
        settling_seconds=0, recovery_deadline_seconds=300, enabled=True, restart=restart,
    )


def test_same_revision_recovery_is_durable_one_shot_and_latches_on_failed_verdict(
    tmp_path, monkeypatch,
):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    source = _degraded(ledger)
    commands = []

    async def restart(scope):
        commands.append(scope)
        return True

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: SHA)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    controller = _controller(tmp_path, ledger, restart)
    outcome = asyncio.run(controller.recover_one())

    assert outcome is not None
    assert outcome.state == AWAITING_VERDICT
    assert commands == ["system"]
    persisted = controller._records.get(source.id)
    assert persisted is not None and persisted.recovery_receipt_id
    staged = ledger.get(persisted.recovery_receipt_id)
    assert staged is not None
    assert staged.expected_sha == SHA and staged.baseline_sha == SHA
    assert staged.baseline_process_id == NEW_PROCESS
    assert asyncio.run(controller.recover_one()) is None

    restarted_runtime = _controller(tmp_path, ledger, restart)
    assert restarted_runtime.autonomy_halted is True
    assert asyncio.run(restarted_runtime.recover_one()) is None

    assert ledger.mark_restart_failed(staged.id).status == DEGRADED
    finished = restarted_runtime.reconcile_verdicts()
    assert [item.state for item in finished] == [FAILED]
    assert restarted_runtime.autonomy_halted is True
    terminal = restarted_runtime.next_incident()
    assert terminal is not None and terminal.state == FAILED
    restarted_runtime.mark_incident_recorded(terminal.source_receipt_id)
    assert restarted_runtime.next_incident() is None


def test_mismatched_revision_or_unmanaged_process_never_writes_intent_or_restarts(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    source = _degraded(ledger)
    commands = []

    async def restart(_scope):
        commands.append(True)
        return True

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: "d" * 40)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    controller = _controller(tmp_path, ledger, restart)
    assert asyncio.run(controller.recover_one()) is None
    assert controller._records.get(source.id) is None

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: SHA)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: False)
    assert asyncio.run(controller.recover_one()) is None
    assert controller._records.get(source.id) is None
    assert commands == []


def test_interrupted_intent_becomes_uncertain_and_halts_after_controller_restart(tmp_path):
    db_path = tmp_path / "state.sqlite"
    ledger = DeployLedger(db_path)
    source = _degraded(ledger)
    records = DeploymentRecoveryLedger(db_path)
    assert records.begin(source) is not None

    async def restart(_scope):
        raise AssertionError("uncertain recovery must never restart")

    controller = _controller(tmp_path, ledger, restart)
    persisted = controller._records.get(source.id)
    assert persisted is not None and persisted.state == UNCERTAIN
    assert controller.autonomy_halted is True
    terminal = controller.next_incident()
    assert terminal is not None and terminal.state == UNCERTAIN


def test_failed_restart_persists_the_terminal_state_and_latch_together(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    source = _degraded(ledger)

    async def restart(_scope):
        return False

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: SHA)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    controller = _controller(tmp_path, ledger, restart)
    outcome = asyncio.run(controller.recover_one())

    assert outcome is not None and outcome.state == FAILED
    assert SafetyStateStore(tmp_path / "state.sqlite").is_latched("deployment_recovery") is True
    assert controller.next_incident() is not None


def test_recovery_rechecks_clean_revision_and_process_immediately_before_restart(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    source = _degraded(ledger)
    revisions = iter((SHA, "d" * 40))
    commands = []

    async def restart(_scope):
        commands.append(True)
        return True

    monkeypatch.setattr(
        "hive.core.deployment_recovery.detect_source_revision", lambda _root: next(revisions),
    )
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    controller = _controller(tmp_path, ledger, restart)
    outcome = asyncio.run(controller.recover_one())

    assert outcome is not None and outcome.state == "suppressed"
    assert commands == []
    assert controller.autonomy_halted is True


def test_completed_healthy_verdict_wins_over_a_late_recovery_deadline(tmp_path):
    db_path = tmp_path / "state.sqlite"
    ledger = DeployLedger(db_path)
    source = _degraded(ledger)
    recovery_receipt = ledger.schedule(
        "run-recovery", "gateway", "systemctl", "host-a", SHA, baseline_sha=SHA,
        now=100, await_restart=True, baseline_process_id=NEW_PROCESS, exclusive=True,
    )
    records = DeploymentRecoveryLedger(db_path)
    assert records.begin(source, now=100) is not None
    staged = records.stage(source.id, recovery_receipt.id, deadline_seconds=1, now=100)
    ledger.confirm_gateway_start("host-a", SHA, "d" * 32, settling_seconds=0, now=101)
    claimed = ledger.claim_due("host-a", "worker", now=160)
    assert claimed is not None
    assert ledger.finish(claimed.id, "worker", HEALTHY, now=161, claim_count=1).status == HEALTHY

    async def restart(_scope):
        raise AssertionError("a completed recovery must not restart")

    controller = _controller(tmp_path, ledger, restart)
    assert controller.autonomy_halted is True
    result = controller.reconcile_verdicts()
    assert [record.state for record in result] == ["resolved"]
    assert controller.autonomy_halted is False
    assert SafetyStateStore(db_path).is_latched("deployment_recovery") is False


def test_deployment_recovery_config_requires_verification_and_approver(tmp_path):
    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), deploy_recovery_enabled=True)
    issues = cfg.validate()
    assert "HIVE_DEPLOY_RECOVERY_ENABLED requires HIVE_DEPLOY_VERIFY_ENABLED=true" in issues
    assert "HIVE_DEPLOY_RECOVERY_ENABLED requires HIVE_APPROVER_KEY" in issues


def test_deployment_recovery_rejects_a_heartbeat_that_cannot_meet_its_deadline(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        deploy_recovery_enabled=True, deploy_verify_enabled=True,
        heartbeat_sec=7200, deploy_verify_settling_sec=1,
    )
    assert "HIVE_DEPLOY_RECOVERY_ENABLED requires heartbeat and settling time within 7200 seconds" in cfg.validate()


def test_durable_recovery_latch_pauses_heartbeat_when_deploy_verification_is_disabled(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver", worker_isolation="required",
        deploy_verify_enabled=False, deploy_recovery_enabled=False,
    )
    hive = HiveOS.build(cfg)
    assert hive.deploy_recovery is None
    SafetyStateStore(cfg.state_db).engage_latch("deployment_recovery", "test:receipt")
    summary = asyncio.run(Heartbeat(hive).tick())
    assert summary["paused"] is True
    assert summary["pause_reason"] == "deployment_recovery"


@pytest.mark.parametrize("recovery_state", ["prepared", "staged", "awaiting_verdict"])
def test_unlatched_recovery_intent_pauses_after_verification_is_disabled(tmp_path, recovery_state):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver", worker_isolation="required",
        deploy_verify_enabled=False, deploy_recovery_enabled=False,
    )
    host_identity = f"{socket.gethostname()}\0{cfg.root.resolve()}"
    host_key = hashlib.sha256(host_identity.encode()).hexdigest()[:32]
    ledger = DeployLedger(cfg.state_db)
    source = _degraded(ledger, host=host_key)
    records = DeploymentRecoveryLedger(cfg.state_db)
    assert records.begin(source) is not None  # prepared, before any durable latch
    if recovery_state != "prepared":
        replacement = ledger.schedule(
            "run-replacement", "gateway", "systemctl", host_key, SHA,
            baseline_sha=SHA, await_restart=True,
            baseline_process_id=NEW_PROCESS, exclusive=True,
        )
        records.stage(source.id, replacement.id, deadline_seconds=300)
        if recovery_state == "awaiting_verdict":
            records.mark_command(source.id, success=True)
    assert not SafetyStateStore(cfg.state_db).is_latched("deployment_recovery")

    hive = HiveOS.build(cfg)
    assert hive.deploy_recovery is not None
    assert hive.deploy_verifier is None
    assert hive.deploy_recovery.autonomy_halted
    assert records.get(source.id).state == (
        UNCERTAIN if recovery_state == "prepared" else recovery_state
    )
    restart_calls = []

    async def forbidden_restart(scope):
        restart_calls.append(scope)
        return True

    hive.deploy_recovery._restart = forbidden_restart
    summary = asyncio.run(Heartbeat(hive).tick())
    assert summary["paused"] is True
    assert summary["pause_reason"] == "deployment_recovery"
    assert restart_calls == []


def test_older_unrelated_degraded_receipts_cannot_starve_gateway_recovery(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    for index in range(8):
        older = ledger.schedule(
            f"older-{index}", "keeper", "systemctl", "host-a", SHA,
            baseline_sha=SHA, now=10 + index, await_restart=True,
            baseline_process_id=OLD_PROCESS,
        )
        ledger.mark_restart_failed(older.id, now=20 + index)
    source = _degraded(ledger)
    calls = []

    async def restart(scope):
        calls.append(scope)
        return True

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: SHA)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    controller = _controller(tmp_path, ledger, restart)
    result = asyncio.run(controller.recover_one())
    assert result is not None and result.source_receipt_id == source.id
    assert result.state == AWAITING_VERDICT
    assert calls == ["system"]


def test_dynamic_mismatches_do_not_starve_later_gateway_recovery(tmp_path, monkeypatch):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    other_sha = "d" * 40
    for index in range(8):
        older = ledger.schedule(
            f"other-{index}", "gateway", "systemctl", "host-a", other_sha,
            baseline_sha=other_sha, now=10 + index, await_restart=True,
            baseline_process_id=OLD_PROCESS, exclusive=True,
        )
        ledger.confirm_gateway_start("host-a", other_sha, NEW_PROCESS, settling_seconds=0, now=11 + index)
        ledger.mark_restart_failed(older.id, now=12 + index)
    source = _degraded(ledger)
    calls = []

    async def restart(scope):
        calls.append(scope)
        return True

    monkeypatch.setattr("hive.core.deployment_recovery.detect_source_revision", lambda _root: SHA)
    monkeypatch.setattr("hive.core.deployment_recovery.is_managed_gateway_process", lambda **_kw: True)
    result = asyncio.run(_controller(tmp_path, ledger, restart).recover_one())
    assert result is not None and result.source_receipt_id == source.id
    assert result.state == AWAITING_VERDICT
    assert calls == ["system"]


@pytest.mark.parametrize("scope", ["user", "system"])
def test_recovery_runner_uses_exact_service_and_strips_approver_key(monkeypatch, scope):
    captured = {}

    class _Process:
        async def wait(self):
            return 0

    async def create(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setenv("HIVE_APPROVER_KEY", "never-pass-this")
    monkeypatch.setenv("SYSTEMCTL_FORCE_BUS", "1")
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/tmp/other-system-bus")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/user-bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/runtime-user")
    monkeypatch.setattr(
        "hive.core.deployment_recovery_runner.asyncio.create_subprocess_exec", create,
    )
    assert asyncio.run(restart_gateway_systemctl(scope)) is True
    assert captured["args"] == (
        "systemctl", f"--{scope}", "restart", "hiveos-gateway.service",
    )
    assert "HIVE_APPROVER_KEY" not in captured["env"]
    assert "SYSTEMCTL_FORCE_BUS" not in captured["env"]
    assert "DBUS_SYSTEM_BUS_ADDRESS" not in captured["env"]

    def probe(*_args, **kwargs):
        captured["probe_env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="123")

    assert is_managed_gateway_process(
        pid=123, platform="linux", scope=scope, runner=probe,
    )
    assert captured["env"] == captured["probe_env"]
