"""discord.py — Discord transport for inbound interactions and webhook replies."""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping

import httpx
from nacl.exceptions import CryptoError
from nacl.signing import VerifyKey

from hive.gateway.channels.base import (
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)

log = logging.getLogger("hive.gateway.discord")

_SIGNATURE_WINDOW_SECONDS = 5 * 60
_API_BASE = "https://discord.com/api/v10"


class DiscordChannel(ChannelAdapter):
    name = "discord"

    def __init__(self, *, bot_token: str = "", public_key: str = "",
                 application_id: str = "", webhook_token: str = "",
                 client: httpx.AsyncClient | None = None,
                 api_base: str = _API_BASE) -> None:
        self._bot_token = bot_token
        self._public_key = public_key
        self._application_id = application_id
        self._webhook_token = webhook_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)
        self._api_base = api_base.rstrip("/")

    def parse_update(self, raw: dict[str, Any]) -> MessageEvent | None:
        if not isinstance(raw, dict):
            return None
        if raw.get("t") == 0:
            return None
        if raw.get("t") != 2:
            return None
        data = raw.get("d")
        if not isinstance(data, dict):
            return None
        author = data.get("author")
        if not isinstance(author, dict):
            return None
        if author.get("bot"):
            return None
        content = data.get("content")
        if not content:
            return None
        channel_id = data.get("channel_id")
        if not channel_id:
            return None
        return MessageEvent(
            text=content,
            chat_id=str(channel_id),
            user_id=str(author.get("id", "")),
            message_id=str(data.get("id", "")),
            platform="discord",
            raw=raw,
        )

    def verify_signature(self, headers: Mapping[str, str], body: bytes,
                         public_key: str | None = None) -> bool:
        key = public_key if public_key is not None else self._public_key
        if not key:
            return False
        sig = headers.get("X-Signature-Ed25519") or headers.get("x-signature-ed25519")
        ts = headers.get("X-Signature-Timestamp") or headers.get("x-signature-timestamp")
        if not sig or not ts:
            return False
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts_int) > _SIGNATURE_WINDOW_SECONDS:
            return False
        try:
            sig_bytes = bytes.fromhex(sig)
            vk = VerifyKey(bytes.fromhex(key))
            vk.verify(ts.encode() + body, sig_bytes)
        except (CryptoError, ValueError, TypeError):
            return False
        return True

    async def send(self, message: OutgoingMessage) -> SendResult:
        if self._webhook_token and self._application_id:
            url = f"{self._api_base}/webhooks/{self._application_id}/{self._webhook_token}"
            params: dict[str, Any] = {}
        elif self._bot_token:
            url = f"{self._api_base}/channels/{message.chat_id}/messages"
            params = {}
        else:
            return SendResult(ok=False, error="discord credentials not configured")
        try:
            r = await self._client.post(
                url,
                params={**params, "wait": "true"},
                json={"content": message.text},
                headers=self._auth_headers(),
            )
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            log.warning("discord send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        if r.status_code >= 400:
            return SendResult(ok=False, error=f"discord http {r.status_code}")
        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=f"discord invalid json: {exc}")
        return SendResult(ok=True, message_id=str(data.get("id", "")))

    def _auth_headers(self) -> dict[str, str]:
        if self._bot_token and not self._webhook_token:
            return {"Authorization": f"Bot {self._bot_token}"}
        return {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
