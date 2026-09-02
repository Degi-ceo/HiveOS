"""Regression tests for conservative durable Telegram ingress state."""
from __future__ import annotations

from hive.gateway.channels.telegram_inbox import (
    AMBIGUOUS,
    REPLIED,
    TelegramInbox,
)


def test_inbox_deduplicates_across_reopen_and_persists_reply(tmp_path):
    now = [100.0]
    db = tmp_path / "telegram.sqlite"
    inbox = TelegramInbox(db, bot_scope="test", clock=lambda: now[0])
    assert inbox.accept(update_id=42, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="9", thread_id="")
    assert not inbox.accept(update_id=42, session_id="telegram:1:7", chat_id="1",
                            user_id="7", message_id="9", thread_id="")
    assert inbox.claim_processing(42, worker_id="ask-worker")
    assert inbox.store_reply(42, worker_id="ask-worker", reply_text="safe reply")
    inbox.close()

    reopened = TelegramInbox(db, bot_scope="test", clock=lambda: now[0])
    record = reopened.get(42)
    assert record is not None and record.reply_text == "safe reply"
    sending = reopened.claim_send(42, worker_id="send-worker")
    assert sending is not None and sending.reply_text == "safe reply"
    assert reopened.mark_replied(42, worker_id="send-worker", receipt="provider-message-42")
    assert reopened.get(42).state == REPLIED
    reopened.close()


def test_inbox_quarantines_expired_inflight_work_without_replay(tmp_path):
    now = [100.0]
    inbox = TelegramInbox(tmp_path / "telegram.sqlite", clock=lambda: now[0])
    assert inbox.accept(update_id=1, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="1", thread_id="")
    assert inbox.claim_processing(1, worker_id="worker", lease_seconds=5)
    now[0] = 106.0
    assert inbox.recover_expired() == 1
    record = inbox.get(1)
    assert record is not None and record.state == AMBIGUOUS
    assert not inbox.claim_processing(1, worker_id="replay-worker")
    inbox.close()


def test_restart_recovery_quarantines_every_nonterminal_state(tmp_path):
    """A new gateway process never resumes work whose prior outcome is unknown."""
    now = [100.0]
    db = tmp_path / "telegram.sqlite"
    inbox = TelegramInbox(db, clock=lambda: now[0])

    assert inbox.accept(update_id=0, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="0", thread_id="")
    assert inbox.accept(update_id=1, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="1", thread_id="")
    assert inbox.claim_processing(1, worker_id="processing", lease_seconds=600)

    assert inbox.accept(update_id=2, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="2", thread_id="")
    assert inbox.claim_processing(2, worker_id="reply", lease_seconds=600)
    assert inbox.store_reply(2, worker_id="reply", reply_text="must not be sent again")

    assert inbox.accept(update_id=3, session_id="telegram:1:7", chat_id="1",
                        user_id="7", message_id="3", thread_id="")
    assert inbox.claim_processing(3, worker_id="send", lease_seconds=600)
    assert inbox.store_reply(3, worker_id="send", reply_text="unknown delivery")
    assert inbox.claim_send(3, worker_id="send", lease_seconds=600) is not None
    inbox.close()

    restarted = TelegramInbox(db, clock=lambda: now[0])
    assert restarted.recover_after_restart() == 4
    assert [restarted.get(update_id).state for update_id in (0, 1, 2, 3)] == [AMBIGUOUS] * 4
    assert restarted.claim_send(2, worker_id="replay") is None
    assert not restarted.claim_processing(1, worker_id="replay")
    assert restarted.summary() == {
        "pending": 0, "processing": 0, "reply_pending": 0, "sending": 0,
        "replied": 0, "ambiguous": 4, "unknown": 0, "total": 4,
        "open": 0, "requires_review": 4,
    }
    restarted.close()


def test_inbox_summary_is_aggregate_only(tmp_path):
    inbox = TelegramInbox(tmp_path / "telegram.sqlite")
    assert inbox.accept(update_id=1, session_id="private-session", chat_id="private-chat",
                        user_id="private-user", message_id="private-message", thread_id="")
    summary = inbox.summary()

    assert summary == {
        "pending": 1, "processing": 0, "reply_pending": 0, "sending": 0,
        "replied": 0, "ambiguous": 0, "unknown": 0, "total": 1,
        "open": 1, "requires_review": 0,
    }
    assert "private" not in str(summary)
    inbox.close()


def test_inbox_requires_a_provider_receipt_before_marking_replied(tmp_path):
    inbox = TelegramInbox(tmp_path / "telegram.sqlite")
    assert inbox.accept(update_id=1, session_id="s", chat_id="c", user_id="u", message_id="m", thread_id="")
    assert inbox.claim_processing(1, worker_id="worker")
    assert inbox.store_reply(1, worker_id="worker", reply_text="reply")
    assert inbox.claim_send(1, worker_id="worker") is not None
    assert not inbox.mark_replied(1, worker_id="worker", receipt="")
    assert inbox.get(1).state == "sending"
    inbox.close()
