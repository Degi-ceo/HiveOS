"""Offline contracts for fail-closed outbound evidence."""
from __future__ import annotations

from hive.core.outbound_delivery import CONFIRMED, REQUIRES_REVIEW, OutboundDeliveryLedger
from hive.core.redact import redact_public_approval_text, redact_public_tool_args


def test_outbound_ledger_stores_only_aggregate_safe_evidence(tmp_path):
    ledger = OutboundDeliveryLedger(tmp_path / "state.sqlite")
    effect = ledger.begin(surface="approved_external_message", provider="telegram")
    assert ledger.confirm(effect, receipt="provider-message-private-42")
    assert ledger.summary() == {
        "pending": 0, "in_flight": 0, "confirmed": 1, "requires_review": 0,
        "unknown": 0, "total": 1, "open": 0, "requires_owner_review": 0,
    }
    raw = ledger._db.execute("SELECT * FROM outbound_delivery_effects").fetchone()
    assert raw["state"] == CONFIRMED
    assert "private" not in str(dict(raw))
    assert raw["receipt_fingerprint"]
    ledger.close()


def test_outbound_ledger_restart_never_replays_an_interrupted_effect(tmp_path):
    path = tmp_path / "state.sqlite"
    ledger = OutboundDeliveryLedger(path)
    effect = ledger.begin(surface="approved_external_message", provider="telegram")
    ledger.close()
    reopened = OutboundDeliveryLedger(path)
    assert reopened.recover_after_restart() == 1
    assert reopened._db.execute("SELECT state FROM outbound_delivery_effects WHERE correlation_id=?", (effect,)).fetchone()[0] == REQUIRES_REVIEW
    assert reopened.summary()["requires_owner_review"] == 1
    reopened.close()


def test_external_message_public_projection_never_exposes_body_or_recipient():
    projected = redact_public_tool_args("external_message", {
        "channel": "telegram", "to": "private-recipient", "body": "private body",
    })
    assert projected == {"channel": "telegram", "recipient_present": True, "body_length": 12}
    assert "private" not in str(projected)
    assert "private" not in redact_public_approval_text("external_message", "send private body to private-recipient")


def test_external_message_approval_history_projection_redacts_reason_and_note():
    from hive.core.approval_enhancements import AuditRecord, DecisionOutcome

    record = AuditRecord(
        id="approval", tool="external_message",
        args={"to": "private-recipient", "body": "private-body"},
        reason="send private-body to private-recipient", kind="danger",
        outcome=DecisionOutcome.APPROVED, decided_at=1, requested_at=1,
        decided_by="owner", note="private-body was approved",
    ).to_dict()
    assert "private" not in str(record)
    assert record["reason"] == "external message content redacted"
    assert record["note"] == "external message content redacted"


def test_external_message_with_ledger_quarantines_a_receiptless_provider_send(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from hive.gateway.channels.base import SendResult
    from hive.tools.builtins import ExternalMessage

    channel = MagicMock()
    channel.send = AsyncMock(return_value=SendResult(ok=True, message_id=""))
    channel.aclose = AsyncMock()
    monkeypatch.setattr("hive.gateway.channels.telegram.TelegramChannel", lambda _token: channel)
    ledger = OutboundDeliveryLedger(tmp_path / "state.sqlite")
    result = asyncio.run(ExternalMessage(telegram_token="token", delivery_ledger=ledger).execute(
        to="private-recipient", body="private-body",
    ))
    assert result.success is False
    assert ledger.summary()["requires_owner_review"] == 1
    assert "private" not in str(ledger.summary())
    ledger.close()
