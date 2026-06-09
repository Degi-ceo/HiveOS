"""
rate_limit.py — parse provider x-ratelimit-* headers into a typed snapshot.

Ported from Hermes agent/rate_limit_tracker.py (HERMES_REFERENCE REUSE-READY #3,
Direct/Zero). Trimmed to the parser + bucket math the router needs; the terminal
formatters are dropped (HiveOS surfaces render their own). The router uses the
per-window `remaining` / `remaining_seconds_now` to proactively cool a credential
before it 429s (resilience, M1).

Header schema (Nous/OpenRouter/OpenAI-compatible; MiniMax follows the same):
    x-ratelimit-limit-{requests,tokens}[-1h]
    x-ratelimit-remaining-{requests,tokens}[-1h]
    x-ratelimit-reset-{requests,tokens}[-1h]   (seconds until reset)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class RateLimitBucket:
    """One rate-limit window (e.g. requests per minute)."""

    limit: int = 0
    remaining: int = 0
    reset_seconds: float = 0.0
    captured_at: float = 0.0

    @property
    def used(self) -> int:
        return max(0, self.limit - self.remaining)

    @property
    def usage_pct(self) -> float:
        if self.limit <= 0:
            return 0.0
        return (self.used / self.limit) * 100.0

    @property
    def remaining_seconds_now(self) -> float:
        """Seconds left until this window resets, adjusted for elapsed time."""
        elapsed = time.time() - self.captured_at
        return max(0.0, self.reset_seconds - elapsed)


@dataclass(slots=True)
class RateLimitState:
    """Full rate-limit state parsed from one response's headers."""

    requests_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    requests_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    captured_at: float = 0.0
    provider: str = ""

    @property
    def has_data(self) -> bool:
        return self.captured_at > 0

    @property
    def age_seconds(self) -> float:
        return float("inf") if not self.has_data else time.time() - self.captured_at

    def hottest(self) -> RateLimitBucket | None:
        """The bucket closest to exhaustion (highest usage_pct), or None if no data."""
        buckets = [b for b in (self.requests_min, self.requests_hour,
                               self.tokens_min, self.tokens_hour) if b.limit > 0]
        return max(buckets, key=lambda b: b.usage_pct, default=None)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_rate_limit_headers(
    headers: Mapping[str, str], provider: str = "",
) -> RateLimitState | None:
    """Parse x-ratelimit-* headers into a RateLimitState. None if none present."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if not any(k.startswith("x-ratelimit-") for k in lowered):
        return None

    now = time.time()

    def _bucket(resource: str, suffix: str = "") -> RateLimitBucket:
        tag = f"{resource}{suffix}"
        return RateLimitBucket(
            limit=_safe_int(lowered.get(f"x-ratelimit-limit-{tag}")),
            remaining=_safe_int(lowered.get(f"x-ratelimit-remaining-{tag}")),
            reset_seconds=_safe_float(lowered.get(f"x-ratelimit-reset-{tag}")),
            captured_at=now,
        )

    return RateLimitState(
        requests_min=_bucket("requests"),
        requests_hour=_bucket("requests", "-1h"),
        tokens_min=_bucket("tokens"),
        tokens_hour=_bucket("tokens", "-1h"),
        captured_at=now,
        provider=provider,
    )
