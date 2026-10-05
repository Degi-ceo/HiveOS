"""M37: one-shot, same-revision deployment recovery remains fail-closed."""

from __future__ import annotations

import asyncio
from dataclasses import replace

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


def test_recovery_runner_uses_exact_service_and_strips_approver_key(monkeypatch):
    captured = {}

    class _Process:
        async def wait(self):
            return 0

    async def create(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setenv("HIVE_APPROVER_KEY", "never-pass-this")
    monkeypatch.setattr(
        "hive.core.deployment_recovery_runner.asyncio.create_subprocess_exec", create,
    )
    assert asyncio.run(restart_gateway_systemctl("user")) is True
    assert captured["args"] == (
        "systemctl", "--user", "restart", "hiveos-gateway.service",
    )
    assert "HIVE_APPROVER_KEY" not in captured["env"]
