"""Pure, non-secret readiness report for the Telegram gateway surface."""
from __future__ import annotations

import re
from typing import Any

_WEBHOOK_SECRET = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def report(config: Any) -> dict[str, object]:
    """Return local Telegram-ingress readiness without exposing configuration values.

    This function does not construct a channel or inbox, open a database, contact
    Telegram, or validate a remote webhook. Counts and booleans are intentional;
    tokens, secrets, IDs, paths, headers, lengths, and exception text are omitted.
    """
    token_configured = bool(getattr(config, "telegram_token", ""))
    secret = str(getattr(config, "telegram_webhook_secret", "") or "")
    webhook_secret_configured = bool(secret)
    webhook_secret_format_valid = bool(_WEBHOOK_SECRET.fullmatch(secret))
    allowed_user_ids = getattr(config, "telegram_allowed_user_ids", frozenset()) or frozenset()
    allowed_chat_ids = getattr(config, "telegram_allowed_chat_ids", frozenset()) or frozenset()
    allowed_user_count = len(allowed_user_ids)

    remediation: list[str] = []
    if not token_configured:
        remediation.append("telegram_token_missing")
    if not webhook_secret_configured:
        remediation.append("telegram_webhook_secret_missing")
    elif not webhook_secret_format_valid:
        remediation.append("telegram_webhook_secret_invalid")
    if not allowed_user_count:
        remediation.append("telegram_allowed_users_missing")

    return {
        "token_configured": token_configured,
        "webhook_secret_configured": webhook_secret_configured,
        "webhook_secret_format_valid": webhook_secret_format_valid,
        "allowed_user_count": allowed_user_count,
        "allowed_chat_restriction_configured": bool(allowed_chat_ids),
        "ingress_ready": not remediation,
        "inbox_mode": "durable-no-replay",
        "outbound_delivery_tested": False,
        "remote_webhook_verified": False,
        "remediation": remediation,
    }
