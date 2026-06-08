"""
failover.py — failover taxonomy (INTERFACE STUB).

Centralized retry/rotate/compress/fallback decision, ported from Hermes
error_classifier.FailoverReason (HERMES_REFERENCE §4). Filled in Phase 2 of the
build plan (SYNTHESIS Part C); llm/router.py will consult classify().
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FailoverReason(enum.Enum):
    OK = "ok"
    AUTH = "auth"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    FORMAT_ERROR = "format_error"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ClassifiedError:
    reason: FailoverReason
    retryable: bool = False
    should_rotate_credential: bool = False
    should_compress: bool = False
    should_fallback: bool = False


def classify(exc: Exception) -> ClassifiedError:  # pragma: no cover - stub
    """Map a provider exception to a failover decision. TODO: port Hermes patterns."""
    raise NotImplementedError("failover.classify is implemented in build Phase 2")
