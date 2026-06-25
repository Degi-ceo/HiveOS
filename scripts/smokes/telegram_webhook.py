"""
Smoke test the Telegram webhook end-to-end (no real Telegram API needed).

Spins up the gateway with a fake TelegramChannel, fires fake Updates through
/telegram/webhook, verifies auth + routing + reply.

Usage:
    python scripts/smokes/telegram_webhook.py
Exit 0 on success, non-zero on any assertion failure.

Verifies:
- 401 on missing/wrong webhook secret
- 200 + reply on valid update (routes through hive.ask())
- 200 handled=False on empty/non-actionable update
- 200 (no crash) on malformed body
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

# Bootstrap test-mode env BEFORE importing hive.*
os.environ.setdefault("HIVE_TELEGRAM_TOKEN", "smoke-token")
os.environ.setdefault("HIVE_TELEGRAM_WEBHOOK_SECRET", "smoke-secret")
os.environ.setdefault("HIVE_API_KEY", "smoke-gateway-key")
for k in ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "HIVE_MNEMOSYNE_HOME"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient
from hive.gateway.app import create_app
from hive.gateway.channels.base import ChannelAdapter, MessageEvent, OutgoingMessage, SendResult


class FakeHive:
    """Minimal HiveOS stub: records ask() calls, returns deterministic echo."""
    config = MagicMock()
    config.telegram_token = "smoke-token"
    config.telegram_webhook_secret = "smoke-secret"
    config.api_key = "smoke-gateway-key"
    config.protocol_version = "1.0"
    config.mcp_servers_env = ""

    async def load_mcp_servers(self): pass
    async def aclose(self): pass

    async def ask(self, text, *, session_id, channel_hint=None):
        return f"[echo] {text} (session={session_id}, hint={channel_hint})"


class FakeTelegramChannel(ChannelAdapter):
    """Captures the OutgoingMessage that the gateway would send back."""
    def __init__(self):
        self.sent: list[OutgoingMessage] = []

    async def start(self): pass
    async def stop(self): pass

    def parse_update(self, update):
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None
        return MessageEvent(
            text=msg.get("text", ""),
            chat_id=msg["chat"]["id"],
            message_id=msg.get("message_id"),
            user_id=msg.get("from", {}).get("id"),
        )

    async def send(self, message: OutgoingMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id=str(len(self.sent)))


async def main() -> int:
    hive = FakeHive()
    telegram = FakeTelegramChannel()
    app = create_app(hive, telegram=telegram)

    with TestClient(app) as client:
        # Test 1: wrong secret → 401
        r = client.post("/telegram/webhook",
                        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                        json={"message": {"text": "ping", "chat": {"id": 42}}})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"
        print(f"  TEST 1 — wrong secret:    HTTP {r.status_code} (expected 401)  ✓")

        # Test 2: missing header → 401
        r = client.post("/telegram/webhook",
                        json={"message": {"text": "ping", "chat": {"id": 42}}})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}"
        print(f"  TEST 2 — missing header:  HTTP {r.status_code} (expected 401)  ✓")

        # Test 3: valid update → 200 + reply via channel
        r = client.post("/telegram/webhook",
                        headers={"X-Telegram-Bot-Api-Secret-Token": "smoke-secret"},
                        json={
                            "update_id": 1,
                            "message": {
                                "message_id": 99,
                                "from": {"id": 12345, "first_name": "Kamil"},
                                "chat": {"id": 42, "type": "private"},
                                "date": 1719339720,
                                "text": "ping",
                            },
                        })
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        assert r.json() == {"ok": True, "handled": True}
        assert len(telegram.sent) == 1
        reply = telegram.sent[0]
        assert reply.chat_id == 42
        assert "[echo] ping" in reply.text
        assert "session=telegram:42" in reply.text
        assert "hint=telegram" in reply.text
        print(f"  TEST 3 — valid update:    HTTP 200, reply sent  ✓")
        print(f"           reply text: {reply.text!r}")

        # Test 4: empty/non-actionable update → 200 handled=False
        r = client.post("/telegram/webhook",
                        headers={"X-Telegram-Bot-Api-Secret-Token": "smoke-secret"},
                        json={"update_id": 2, "edited_message": None})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "handled": False}
        print(f"  TEST 4 — empty update:    HTTP 200, handled=False  ✓")

        # Test 5: malformed body → 200 (no crash, no reply)
        r = client.post("/telegram/webhook",
                        headers={"X-Telegram-Bot-Api-Secret-Token": "smoke-secret"},
                        data="not-json-at-all")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "handled": False}
        print(f"  TEST 5 — malformed body:  HTTP 200, handled=False  ✓")

    print("\n  ALL 5 TESTS PASS — Telegram webhook is wired and functional.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
