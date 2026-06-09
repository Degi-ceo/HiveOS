"""
failover.py — centralized failover taxonomy + one decision tree.

Ported from Hermes error_classifier.FailoverReason (docs/references/HERMES_REFERENCE.md
§4): map any provider exception to a single ClassifiedError carrying the
retry / rotate-credential / compress / fallback-model decision, so the router has
*one* decision tree instead of scattered if-checks. RetryPolicy provides jittered
exponential backoff (Hermes retry_utils).
"""
from __future__ import annotations

import enum
import random
from dataclasses import dataclass

import httpx


class FailoverReason(enum.Enum):
    OK = "ok"
    AUTH = "auth"                    # 401/403 — key invalid
    BILLING = "billing"             # 402 — out of credits
    RATE_LIMIT = "rate_limit"       # 429 — slow down / rotate
    CONTEXT_OVERFLOW = "context_overflow"  # input too long — compress
    OVERLOADED = "overloaded"       # 5xx / transport — retry, then fallback
    TIMEOUT = "timeout"             # request timed out
    FORMAT_ERROR = "format_error"   # 400 bad request (non-context)
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ClassifiedError:
    reason: FailoverReason
    retryable: bool = False
    should_rotate_credential: bool = False
    should_compress: bool = False
    should_fallback: bool = False
    status: int | None = None
    detail: str = ""


# reason -> (retryable, rotate, compress, fallback). The single source of policy.
_POLICY: dict[FailoverReason, tuple[bool, bool, bool, bool]] = {
    FailoverReason.AUTH:             (True,  True,  False, True),
    FailoverReason.BILLING:          (False, True,  False, True),
    FailoverReason.RATE_LIMIT:       (True,  True,  False, True),
    FailoverReason.CONTEXT_OVERFLOW: (False, False, True,  True),
    FailoverReason.OVERLOADED:       (True,  False, False, True),
    FailoverReason.TIMEOUT:          (True,  False, False, True),
    FailoverReason.FORMAT_ERROR:     (False, False, False, False),
    FailoverReason.UNKNOWN:          (False, False, False, True),
}

_CONTEXT_HINTS = ("context length", "context window", "too long", "max_tokens",
                  "maximum context", "tokens exceed")


def _reason_for_status(status: int, body: str) -> FailoverReason:
    if status in (401, 403):
        return FailoverReason.AUTH
    if status == 402:
        return FailoverReason.BILLING
    if status == 429:
        return FailoverReason.RATE_LIMIT
    if status in (408, 504):
        return FailoverReason.TIMEOUT
    if status == 413:
        return FailoverReason.CONTEXT_OVERFLOW
    if status == 400:
        low = body.lower()
        return FailoverReason.CONTEXT_OVERFLOW if any(h in low for h in _CONTEXT_HINTS) else FailoverReason.FORMAT_ERROR
    if status >= 500:
        return FailoverReason.OVERLOADED
    return FailoverReason.UNKNOWN


def classify(exc: Exception) -> ClassifiedError:
    """Map a provider exception to the failover decision."""
    status: int | None = None
    detail = str(exc)

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.text[:500]
        except Exception:  # noqa: BLE001 - body may be unreadable
            pass
        reason = _reason_for_status(status, detail)
    elif isinstance(exc, httpx.TimeoutException):
        reason = FailoverReason.TIMEOUT
    elif isinstance(exc, httpx.TransportError):
        # connect/read/network failures: transient, treat like overloaded
        reason = FailoverReason.OVERLOADED
    else:
        reason = FailoverReason.UNKNOWN

    retryable, rotate, compress, fallback = _POLICY[reason]
    return ClassifiedError(
        reason=reason, retryable=retryable, should_rotate_credential=rotate,
        should_compress=compress, should_fallback=fallback, status=status, detail=detail,
    )


@dataclass(slots=True)
class RetryPolicy:
    """Jittered exponential backoff (Hermes retry_utils)."""
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def backoff(self, attempt: int) -> float:
        """Seconds to wait before retry *attempt* (0-indexed), full-jittered."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
        return ceiling * (0.5 + random.random() / 2)
