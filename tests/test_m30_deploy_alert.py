"""Bounded deployment alerts must never reveal the Telegram bot token."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hive.core.config import HiveConfig
from hive.core.deployment_ledger import HEALTHY, DeployLedger
from hive.core.deployment_verifier import DeploymentVerifier
from hive.autonomy.deployment_alert import DeploymentAlert
from hive.autonomy.heartbeat import Heartbeat
from hive.gateway.channels.base import OutgoingMessage, SendResult
from hive.gateway.channels.telegram import TelegramChannel
from hive.runtime import HiveOS


def test_telegram_transport_error_does_not_log_token_or_raw_exception(caplog):
    token = "test-secret-bot-token"

    class FailingClient:
        async def post(self, url, **_kwargs):
            raise RuntimeError(f"request failed at {url}; raw-secret-bearing-error")

    channel = TelegramChannel(token, client=FailingClient())
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(channel.send(OutgoingMessage(chat_id="123", text="safe")))
    assert result.ok is False
    assert result.error == "transport_failure"
    assert token not in caplog.text
    assert "raw-secret-bearing-error" not in caplog.text
    assert token not in result.error


def test_telegram_api_error_does_not_return_untrusted_description():
    class RejectedResponse:
        def json(self):
            return {"ok": False, "description": "raw-secret-bearing-error"}

    class RejectedClient:
        async def post(self, _url, **_kwargs):
            return RejectedResponse()

    channel = TelegramChannel("test-secret-bot-token", client=RejectedClient())
    result = asyncio.run(channel.send(OutgoingMessage(chat_id="123", text="safe")))
    assert result.ok is False
    assert result.error == "telegram_rejected"


def test_telegram_callback_error_does_not_log_token(caplog):
    token = "test-secret-bot-token"

    class FailingClient:
        async def post(self, url, **_kwargs):
            raise RuntimeError(f"request failed at {url}")

    channel = TelegramChannel(token, client=FailingClient())
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(channel.answer_callback("id", "done"))
    assert ok is False
    assert token not in caplog.text


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"ok": True, "result": {}}, "telegram_malformed_response"),
    (200, {"ok": True, "result": {"message_id": 7, "chat": {"id": 456}}},
     "telegram_chat_mismatch"),
    (500, {"ok": True, "result": {"message_id": 7, "chat": {"id": 123}}},
     "telegram_rejected"),
])
def test_telegram_requires_http_and_chat_acknowledgement(status, payload, expected):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(status, json=payload),
    ))
    channel = TelegramChannel("test-secret-bot-token", client=client)
    result = asyncio.run(channel.send(OutgoingMessage(chat_id="123", text="safe")))
    assert result.ok is False
    assert result.error == expected


def test_telegram_send_uses_real_loopback_http_without_external_delivery(caplog):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - HTTP handler API
            size = int(self.headers["Content-Length"])
            seen.append((self.path, json.loads(self.rfile.read(size))))
            body = json.dumps({"ok": True, "result": {
                "message_id": 42, "chat": {"id": 123},
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async def send():
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    channel = TelegramChannel(
                        "test-bot-token", client=client,
                        base_url=f"http://127.0.0.1:{server.server_port}",
                    )
                    return await channel.send(OutgoingMessage(chat_id="123", text="safe"))

            with caplog.at_level(logging.DEBUG), caplog.at_level(
                logging.DEBUG, logger="httpcore",
            ):
                result = asyncio.run(send())
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert result.ok is True
    assert result.message_id == "42"
    assert len(seen) == 1
    assert seen[0][0].endswith("/bottest-bot-token/sendMessage")
    assert seen[0][1] == {"chat_id": "123", "text": "safe"}
    assert "test-bot-token" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"deploy_alert_chat_id": "-123"},
    {"deploy_alert_chat_id": "123", "telegram_allowed_chat_ids": frozenset()},
    {"deploy_alert_chat_id": "123", "telegram_allowed_user_ids": frozenset()},
    {"deploy_alert_chat_id": "123", "telegram_token": ""},
    {"deploy_alert_chat_id": "123", "deploy_verify_enabled": False},
])
def test_alert_recipient_configuration_fails_closed(tmp_path, changes):
    values = {
        "autonomy_enabled": True, "approver_key": "test-approver",
        "worker_isolation": "required", "deploy_verify_enabled": True,
        "telegram_token": "test-bot-token",
        "telegram_allowed_user_ids": frozenset({"123"}),
        "telegram_allowed_chat_ids": frozenset({"123"}),
    }
    values.update(changes)
    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), **values)
    with pytest.raises(RuntimeError, match="HIVE_DEPLOY_ALERT_CHAT_ID"):
        HiveOS.build(cfg)


def test_alert_chat_id_loads_from_env_but_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DEPLOY_ALERT_CHAT_ID", "123")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.deploy_alert_chat_id == "123"
    assert cfg.to_safe_dict()["deploy_alert_chat_id"] == "***"


def test_degraded_alert_claims_stop_after_three_leases(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    receipt = ledger.schedule(
        "run-30", "gateway", "systemctl", "host-a", "a" * 40, now=100,
    )
    ledger.mark_restart_failed(receipt.id, now=101)
    for generation in (1, 2, 3):
        claimed = DeployLedger(tmp_path / "deploy.sqlite").claim_alert(
            "host-a", f"owner-{generation}", now=110 + 10 * generation,
            lease_seconds=5,
        )
        assert claimed.id == receipt.id
        assert claimed.alert_claim_count == generation
    assert ledger.claim_alert("host-a", "owner-4", now=200) is None
    assert ledger.get(receipt.id).alert_sent_at is None
    assert ledger.get(receipt.id).alert_exhausted_at == 200
    assert ledger.next_alert_failure_without_incident("host-a", now=201) is None


def test_live_alert_claim_skips_legacy_and_unverified_receipts(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    legacy = ledger.schedule("old-run", "gateway", "systemctl", "host-a", "a" * 40,
                             now=100)
    ledger.mark_restart_failed(legacy.id, now=101)
    live = ledger.schedule(
        "new-run", "gateway", "systemctl", "host-a", "b" * 40,
        now=102, await_restart=True, baseline_process_id="c" * 32,
        exclusive=True,
    )
    ledger.mark_restart_failed(live.id, now=103)
    claimed = ledger.claim_alert("host-a", "alerter", now=104, live_only=True)
    assert claimed.id == live.id
    assert ledger.get(legacy.id).alert_claim_count == 0


def test_diff_baseline_uses_only_same_scope_process_bound_healthy_receipt(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")

    def healthy_live(sha, scope, now):
        item = ledger.schedule(
            f"run-{scope}", "gateway", "systemctl", "host-a", sha,
            now=now, await_restart=True, baseline_process_id="c" * 32,
            exclusive=True, systemctl_scope=scope,
        )
        ledger.confirm_gateway_start(
            "host-a", sha, "d" * 32, now=now + 1,
            systemctl_scope=scope,
        )
        claim = ledger.claim_due("host-a", f"worker-{scope}", now=now + 60)
        ledger.finish(item.id, f"worker-{scope}", HEALTHY,
                      now=now + 61, claim_count=claim.claim_count)

    healthy_live("a" * 40, "system", 100)
    healthy_live("b" * 40, "user", 200)
    legacy = ledger.schedule("legacy", "gateway", "systemctl", "host-a",
                             "e" * 40, now=300)
    claim = ledger.claim_due("host-a", "legacy-worker", now=330)
    ledger.finish(legacy.id, "legacy-worker", HEALTHY, now=331,
                  claim_count=claim.claim_count)
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") == "e" * 40
    assert ledger.last_healthy_sha(
        "host-a", "gateway", "systemctl", systemctl_scope="system",
    ) == "a" * 40
    assert ledger.last_healthy_sha(
        "host-a", "gateway", "systemctl", systemctl_scope="user",
    ) == "b" * 40


def test_final_failed_claim_is_durably_exhausted(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    receipt = ledger.schedule("run-30", "gateway", "systemctl", "host-a", "a" * 40,
                              now=100)
    ledger.mark_restart_failed(receipt.id, now=101)
    last = None
    for generation in (1, 2, 3):
        last = ledger.claim_alert("host-a", "alerter", now=110 * generation,
                                  lease_seconds=5)
    exhausted = ledger.mark_alert_exhausted(
        receipt.id, "alerter", now=331, claim_count=last.alert_claim_count,
    )
    assert exhausted.alert_exhausted_at == 331
    assert exhausted.alert_sent_at is None
    assert DeployLedger(tmp_path / "deploy.sqlite").claim_alert(
        "host-a", "another", now=500,
    ) is None


def _live_failed_verifier(tmp_path, clock):
    ledger = DeployLedger(tmp_path / "deploy.sqlite", clock=lambda: clock[0])
    receipt = ledger.schedule(
        "run-30", "gateway", "systemctl", "host-a", "a" * 40,
        baseline_sha="b" * 40, now=100, await_restart=True,
        baseline_process_id="c" * 32, exclusive=True,
    )
    ledger.mark_restart_failed(receipt.id, now=101)

    async def healthy(_record):
        return True

    async def revision(_record):
        return "a" * 40

    verifier = DeploymentVerifier(
        ledger, host_key="host-a", owner="verifier", doctor=healthy,
        gateway=healthy, smoke=healthy, revision=revision,
    )
    return ledger, verifier, receipt


def test_three_crashes_before_http_exhaust_live_receipt_and_replay_incident(tmp_path):
    clock = [102]
    ledger, _verifier, receipt = _live_failed_verifier(tmp_path, clock)
    # Each claim simulates a different process dying before it can send.
    for generation, timestamp in enumerate((102, 163, 224), 1):
        claimed = DeployLedger(
            tmp_path / "deploy.sqlite", clock=lambda: clock[0],
        ).claim_alert("host-a", f"crashed-{generation}", now=timestamp,
                      lease_seconds=60, live_only=True)
        assert claimed.id == receipt.id
    replay = DeployLedger(tmp_path / "deploy.sqlite").next_alert_failure_without_incident(
        "host-a", now=285,
    )
    assert replay.id == receipt.id
    assert replay.alert_sent_at is None
    assert replay.alert_exhausted_at == 285
    assert ledger.mark_alert_failure_recorded(receipt.id, "host-a", now=286).alert_failure_recorded_at == 286
    assert ledger.next_alert_failure_without_incident("host-a", now=287) is None


def test_alert_sends_only_safe_receipt_evidence_and_marks_acknowledgement(tmp_path):
    clock = [102]
    ledger, verifier, receipt = _live_failed_verifier(tmp_path, clock)
    sent = []

    async def sender(message):
        sent.append(message)
        return SendResult(ok=True, message_id="7")

    alert = DeploymentAlert(verifier, chat_id="1234567890123456789", owner="Degi-ceo",
                            repo="HiveOS", sender=sender)
    assert asyncio.run(alert.check()) is True
    assert len(sent) == 1
    assert sent[0].chat_id == "1234567890123456789"
    assert f"Receipt: {receipt.id}" in sent[0].text
    assert "Failing signals: restart" in sent[0].text
    assert "https://github.com/Degi-ceo/HiveOS/compare/" in sent[0].text
    assert "1234567890123456789" not in sent[0].text
    assert ledger.get(receipt.id).alert_sent_at == 102
    assert asyncio.run(alert.check()) is False


def test_ambiguous_delivery_can_duplicate_but_is_bounded_across_restart(tmp_path):
    clock = [102]
    ledger, verifier, receipt = _live_failed_verifier(tmp_path, clock)
    messages = []

    async def unknown(message):
        messages.append(message.text)
        raise TimeoutError("raw-secret-bearing-error")

    alert = DeploymentAlert(verifier, chat_id="123", owner="Degi-ceo",
                            repo="HiveOS", sender=unknown)
    assert asyncio.run(alert.check()) is False
    assert ledger.get(receipt.id).alert_claim_count == 1
    assert ledger.get(receipt.id).alert_sent_at is None
    assert asyncio.run(alert.check()) is False  # active lease blocks duplicate
    clock[0] = 163
    restarted = DeploymentAlert(verifier, chat_id="123", owner="Degi-ceo",
                                repo="HiveOS", sender=unknown)
    assert asyncio.run(restarted.check()) is False
    clock[0] = 224
    assert asyncio.run(restarted.check()) is False
    assert ledger.get(receipt.id).alert_exhausted_at == 224
    clock[0] = 300
    assert asyncio.run(restarted.check()) is False
    assert len(messages) == 3
    assert all(f"Receipt: {receipt.id}" in message for message in messages)


def test_heartbeat_dispatches_alert_before_budget_pause(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
        telegram_token="test-bot-token", telegram_webhook_secret="test-webhook",
        deploy_alert_chat_id="123",
        telegram_allowed_user_ids=frozenset({"123"}),
        telegram_allowed_chat_ids=frozenset({"123"}),
    )
    hive = HiveOS.build(cfg)
    called = []

    class FakeAlert:
        async def check(self):
            called.append("alert")
            return False

    monkeypatch.setattr(
        "hive.autonomy.deployment_alert.make_deployment_alert",
        lambda _hive: FakeAlert(),
    )
    monkeypatch.setattr(hive.budgeter, "daily_spend_status", lambda: {
        "hard_cap_reached": True, "cost_usd": 1.0, "cap_usd": 1.0,
    })
    summary = asyncio.run(Heartbeat(hive).tick())
    assert summary["paused"] is True
    assert called == ["alert"]


def test_exhausted_delivery_creates_durable_operator_incident(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
        telegram_token="test-bot-token", telegram_webhook_secret="test-webhook",
        deploy_alert_chat_id="123",
        telegram_allowed_user_ids=frozenset({"123"}),
        telegram_allowed_chat_ids=frozenset({"123"}),
    )
    hive = HiveOS.build(cfg)
    ledger = hive.tools["deploy"]._deploy_ledger
    host_key = hive.tools["deploy"]._deploy_host_key
    clock = [102]
    ledger._clock = lambda: clock[0]
    receipt = ledger.schedule(
        "run-alert-fail", "gateway", "systemctl", host_key, "a" * 40,
        now=100, await_restart=True, baseline_process_id="c" * 32,
        exclusive=True,
    )
    ledger.mark_restart_failed(receipt.id, now=101)

    async def unknown(_message):
        return SendResult(ok=False, error="transport_failure")

    monkeypatch.setattr(
        "hive.autonomy.deployment_alert.make_deployment_alert",
        lambda _hive: DeploymentAlert(
            hive.deploy_verifier, chat_id="123", owner="Degi-ceo",
            repo="HiveOS", sender=unknown,
        ),
    )
    monkeypatch.setattr(hive.budgeter, "daily_spend_status", lambda: {
        "hard_cap_reached": True, "cost_usd": 1.0, "cap_usd": 1.0,
    })
    heartbeat = Heartbeat(hive)
    for timestamp in (102, 163, 224):
        clock[0] = timestamp
        assert asyncio.run(heartbeat.tick())["paused"] is True
    exhausted = ledger.get(receipt.id)
    assert exhausted.alert_exhausted_at == 224
    assert exhausted.alert_failure_recorded_at == 224
    with hive.incident_ledger._lock:
        row = hive.incident_ledger._db.execute(
            "SELECT source, run_id FROM hive_incidents WHERE source=?",
            ("deploy_alert",),
        ).fetchone()
    assert tuple(row) == ("deploy_alert", "run-alert-fail")
