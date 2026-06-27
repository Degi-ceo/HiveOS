"""email.py — Email transport: parse RFC822 inbound, aiosmtplib send outbound."""
from __future__ import annotations

import email
import email.policy
import logging
from email.message import EmailMessage as _StdEmailMessage
from typing import Any, Mapping

import aiosmtplib

from hive.gateway.channels.base import (
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)

log = logging.getLogger("hive.gateway.email")


class EmailChannel(ChannelAdapter):
    name = "email"

    def __init__(self, *, smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = "", smtp_from: str = "",
                 starttls: bool = True) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._user = smtp_user
        self._pass = smtp_pass
        self._from = smtp_from or smtp_user
        self._starttls = starttls

    def parse_update(self, raw: dict[str, Any]) -> MessageEvent | None:
        if not isinstance(raw, dict):
            return None
        body_bytes = raw.get("raw_bytes")
        if not isinstance(body_bytes, (bytes, bytearray)):
            return None
        msg = email.message_from_bytes(bytes(body_bytes), policy=email.policy.default)
        text = _extract_text(msg)
        if not text:
            return None
        subject = msg.get("Subject", "")
        full_text = f"{subject}\n\n{text}" if subject and subject != text else text
        from_addr = msg.get("From", "")
        message_id = msg.get("Message-ID", "")
        in_reply_to = msg.get("In-Reply-To", "")
        chat_id = from_addr or message_id or "unknown"
        return MessageEvent(
            text=full_text,
            chat_id=chat_id,
            user_id=from_addr,
            message_id=message_id,
            platform="email",
            raw={"in_reply_to": in_reply_to, "subject": subject},
        )

    def verify_signature(self, headers: Mapping[str, str], body: bytes,
                         *args: Any, **kwargs: Any) -> bool:
        # v1: gateway authenticates POSTs to /email/webhook via X-Webhook-Secret;
        # DKIM is out of scope. Always accept parsed messages.
        return True

    async def send(self, message: OutgoingMessage) -> SendResult:
        if not self._host or not self._from:
            return SendResult(ok=False, error="smtp host/from not configured")
        mime = _StdEmailMessage()
        mime["From"] = self._from
        mime["To"] = message.chat_id
        mime["Subject"] = message.text.splitlines()[0][:78] if message.text else "(no subject)"
        if message.reply_to:
            mime["In-Reply-To"] = message.reply_to
            mime["References"] = message.reply_to
        body_text = message.text.split("\n\n", 1)[1] if "\n\n" in (message.text or "") else (message.text or "")
        mime.set_content(body_text or message.text or "")
        try:
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._user or None,
                password=self._pass or None,
                start_tls=self._starttls,
            )
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            log.warning("email send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        return SendResult(ok=True, message_id=mime.get("Message-ID", ""))

    async def aclose(self) -> None:
        return None


def _extract_text(msg: Any) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_content()
                if isinstance(payload, str) and payload.strip():
                    return payload
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and "attachment" not in (part.get("Content-Disposition") or "").lower():
                payload = part.get_content()
                if isinstance(payload, str) and payload.strip():
                    return payload
        return ""
    payload = msg.get_content() if hasattr(msg, "get_content") else str(msg.get_payload() or "")
    return payload if isinstance(payload, str) else ""
