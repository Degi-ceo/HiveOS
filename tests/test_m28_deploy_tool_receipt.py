"""Approved local gateway restart gets a durable, attributable receipt."""

from __future__ import annotations

import asyncio
import re

from hive.core.deployment_ledger import DEGRADED, HEALTHY, PENDING, DeployLedger
from hive.core.deployment_verifier import DeploymentVerifier
from hive.core.run_context import bind_run_id
from hive.core.types import ToolResult
from hive.tools.builtins import Deploy

SHA = "a" * 40


def _tool(tmp_path, monkeypatch, *, restart_ok=True, revision=SHA):
    ledger = DeployLedger(tmp_path / "state.sqlite")
    monkeypatch.setattr("hive.core.revision.detect_source_revision", lambda _root: revision)
    calls = []

    async def restart(self, cmd, timeout=30.0):
        calls.append(cmd)
        return ToolResult(tool_name="deploy", success=restart_ok,
                          content="ok" if restart_ok else "exit 1")

    monkeypatch.setattr(Deploy, "_run_cmd", restart)
    tool = Deploy(deploy_ledger=ledger, host_key="host-a", repo_root=tmp_path,
                  settling_seconds=5)
    return tool, ledger, calls


def test_successful_restart_schedules_correlated_verification(tmp_path, monkeypatch):
    tool, ledger, calls = _tool(tmp_path, monkeypatch)
    with bind_run_id("run-141"):
        result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is True
    assert calls == [("systemctl", "restart", "hiveos-gateway.service")]
    match = re.search(r"receipt=([0-9a-f-]{36})", result.content)
    assert match is not None
    receipt = ledger.get(match.group(1))
    assert receipt.status == PENDING
    assert receipt.run_id == "run-141"
    assert receipt.expected_sha == SHA
    assert receipt.host_key == "host-a"


def test_failed_restart_records_degraded_signal(tmp_path, monkeypatch):
    tool, ledger, _ = _tool(tmp_path, monkeypatch, restart_ok=False)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is False
    receipt_id = result.content.rsplit("receipt=", 1)[1]
    receipt = ledger.get(receipt_id)
    assert receipt.status == DEGRADED
    assert receipt.failed_signals == ("restart",)


def test_unverifiable_revision_refuses_restart(tmp_path, monkeypatch):
    tool, _ledger, calls = _tool(tmp_path, monkeypatch, revision=None)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is False
    assert "revision unavailable" in result.content
    assert calls == []


def test_other_mode_remains_explicitly_unverified(tmp_path, monkeypatch):
    tool, _ledger, calls = _tool(tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(target="keeper", mode="systemctl"))
    assert result.success is True
    assert "unverified" in result.content
    assert calls == [("systemctl", "restart", "hiveos-keeper.service")]


def test_restart_child_does_not_inherit_approver_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "test-privileged-key")
    captured = {}

    class FakeProcess:
        returncode = 0
        stdout = object()

        async def communicate(self):
            return b"ok", None

    async def spawn(*_args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(Deploy()._run_cmd(("systemctl", "status", "hiveos-gateway.service")))
    assert result.success is True
    assert not any(key.casefold() == "hive_approver_key" for key in captured)


def test_receipt_is_verified_after_process_restart(tmp_path, monkeypatch):
    tool, ledger, _ = _tool(tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    receipt_id = result.content.rsplit("receipt=", 1)[1]
    receipt = ledger.get(receipt_id)

    async def healthy(_record):
        return True

    async def revision(_record):
        return SHA

    reopened = DeployLedger(tmp_path / "state.sqlite")
    verifier = DeploymentVerifier(
        reopened, host_key="host-a", owner="new-process",
        doctor=healthy, gateway=healthy, smoke=healthy, revision=revision,
    )
    # Do not sleep through the settling window: use a restarted ledger with a
    # deterministic clock after the persisted due timestamp.
    reopened._clock = lambda: receipt.due_at + 1
    verdict = asyncio.run(verifier.verify_due())
    assert verdict.id == receipt_id
    assert verdict.status == HEALTHY
    assert DeployLedger(tmp_path / "state.sqlite").get(receipt_id).status == HEALTHY


def test_unexpected_restart_error_marks_receipt_degraded(tmp_path, monkeypatch):
    tool, ledger, _ = _tool(tmp_path, monkeypatch)

    async def broken(_self, _cmd, timeout=30.0):
        raise RuntimeError("raw-secret-bearing-error")

    monkeypatch.setattr(Deploy, "_run_cmd", broken)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    assert result.success is False
    assert "raw-secret-bearing-error" not in result.content
    assert ledger.get(result.content.rsplit("receipt=", 1)[1]).failed_signals == ("restart",)


def test_verifier_cannot_claim_while_restart_is_still_running(tmp_path, monkeypatch):
    tool, ledger, _ = _tool(tmp_path, monkeypatch)
    tool._deploy_settling_seconds = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_failure(_self, _cmd, timeout=30.0):
        started.set()
        await release.wait()
        return ToolResult(tool_name="deploy", success=False, content="exit 1")

    monkeypatch.setattr(Deploy, "_run_cmd", delayed_failure)

    async def healthy(_record):
        return True

    async def revision(_record):
        return SHA

    verifier = DeploymentVerifier(
        ledger, host_key="host-a", owner="watcher", doctor=healthy,
        gateway=healthy, smoke=healthy, revision=revision,
    )

    async def scenario():
        running = asyncio.create_task(tool.execute(target="gateway", mode="systemctl"))
        await started.wait()
        assert await verifier.verify_due() is None
        release.set()
        return await running

    result = asyncio.run(scenario())
    assert result.success is False
    receipt = ledger.get(result.content.rsplit("receipt=", 1)[1])
    assert receipt.status == DEGRADED
    assert receipt.restart_confirmed_at is None


def test_successful_restart_starts_settling_after_command(tmp_path, monkeypatch):
    tool, ledger, _ = _tool(tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(target="gateway", mode="systemctl"))
    receipt = ledger.get(result.content.rsplit("receipt=", 1)[1])
    assert receipt.restart_confirmed_at is not None
    assert receipt.due_at == receipt.restart_confirmed_at + 5
