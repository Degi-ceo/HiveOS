"""
redact.py — mask secrets before they reach logs / the audit trail (M7 B2).

Trimmed port of Hermes agent/redact.py: regex masking of the common secret shapes
(env assignments, auth headers, JWTs, private keys, vendor token prefixes) plus
key-aware masking of structured tool args. Applied by observability/audit.py before
persisting a tool call, and usable anywhere a string is about to be logged.

DAG: core leaf (stdlib only).
"""
from __future__ import annotations

import re
from typing import Any

# Exact-match (case-insensitive) arg/field names whose VALUE is always a secret.
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "token", "access_token", "refresh_token", "id_token",
    "client_secret", "password", "passwd", "secret", "authorization", "auth",
    "jwt", "private_key", "credential", "signature", "code", "x_api_key",
})

# Vendor token prefixes (OpenAI/GitHub/Slack/AWS/MiniMax-ish/Telegram bot, etc.).
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])("
    r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|\d{6,}:[A-Za-z0-9_-]{30,}"
    r")(?![A-Za-z0-9_-])")
_ENV_ASSIGN_RE = re.compile(
    r"((?:[A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*)\s*[=:]\s*)"
    r"(\S+)", re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)(\S+)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)

_MASK = "***REDACTED***"


def mask_secret(token: str) -> str:
    """Fully mask short tokens; keep first6…last4 of long ones for debuggability."""
    if len(token) < 18:
        return _MASK
    return f"{token[:6]}…{token[-4:]}"


def redact_text(text: str) -> str:
    """Mask secret shapes in free text (logs, error strings, command lines)."""
    if not text:
        return text
    text = _PRIVATE_KEY_RE.sub(_MASK, text)
    text = _ENV_ASSIGN_RE.sub(lambda m: m.group(1) + mask_secret(m.group(2)), text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _MASK, text)
    text = _JWT_RE.sub(_MASK, text)
    text = _PREFIX_RE.sub(lambda m: mask_secret(m.group(1)), text)
    return text


def redact_value(value: Any, *, key: str = "", _depth: int = 0) -> Any:
    """Recursively redact a value. A sensitive KEY masks the whole value; strings
    are scrubbed for secret shapes; dicts/lists recurse (max depth 50)."""
    if key and key.lower().replace("-", "_") in _SENSITIVE_KEYS:
        return _MASK if value not in (None, "", [], {}) else value
    if _depth >= 50:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, _depth=_depth + 1) for v in value]
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Redact a tool-args dict before it is logged/persisted."""
    return {k: redact_value(v, key=str(k), _depth=0) for k, v in (args or {}).items()}
