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
    assert reopened.mark_replied(42, worker_id="send-worker")
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
