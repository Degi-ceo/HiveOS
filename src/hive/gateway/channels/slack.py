"""slack.py — Slack transport for inbound events and chat.postMessage replies."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Mapping

import httpx

from hive.gateway.channels.base import (
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)

log = logging.getLogger("hive.gateway.slack")

_SIGNATURE_WINDOW_SECONDS = 5 * 60


class SlackChannel(ChannelAdapter):
    name = "slack"

    def __init__(self, bot_token: str = "", *, signing_secret: str = "",
                 client: httpx.AsyncClient | None = None,
                 api_base: str = "https://slack.com/api") -> None:
        self._bot_token = bot_token
        self._signing_secret = signing_secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)
        self._api_base = api_base.rstrip("/")

    def parse_update(self, raw: dict[str, Any]) -> MessageEvent | None:
        if not isinstance(raw, dict):
            return None
        if raw.get("type") == "url_verification":
            return None
        if raw.get("type") != "event_callback":
            return None
        event = raw.get("event")
        if not isinstance(event, dict):
            return None
        if event.get("type") != "message":
            return None
        if event.get("subtype") is not None:
            return None
        text = event.get("text")
        channel = event.get("channel")
        if not text or not channel:
            return None
        return MessageEvent(
            text=text,
            chat_id=str(channel),
            user_id=str(event.get("user", "")),
            message_id=str(event.get("ts", "")),
            platform="slack",
            raw=raw,
        )

    @staticmethod
    def verify_signature(headers: Mapping[str, str], body: bytes,
                         signing_secret: str) -> bool:
        if not signing_secret:
            return False
        header_sig = headers.get("X-Slack-Signature") or headers.get("x-slack-signature")
        timestamp = headers.get("X-Slack-Request-Timestamp") or headers.get("x-slack-request-timestamp")
        if not header_sig or not timestamp:
            return False
        try:
            ts_int = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts_int) > _SIGNATURE_WINDOW_SECONDS:
            return False
        sig = header_sig[3:] if header_sig.startswith("v0=") else header_sig
        base = f"v0:{timestamp}:".encode() + body
        digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, digest)

    async def send(self, message: OutgoingMessage) -> SendResult:
        if not self._bot_token:
            return SendResult(ok=False, error="slack bot token not configured")
        payload: dict[str, Any] = {
            "channel": message.chat_id,
            "text": message.text,
        }
        if message.reply_to:
            payload["thread_ts"] = message.reply_to
        try:
            r = await self._client.post(
                f"{self._api_base}/chat.postMessage",
                json=payload,
                headers={"Authorization": f"Bearer {self._bot_token}",
                         "Content-Type": "application/json; charset=utf-8"},
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            log.warning("slack send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        if not data.get("ok"):
            return SendResult(ok=False, error=str(data.get("error", "slack send failed")))
        return SendResult(ok=True, message_id=str(data.get("ts", "")))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
