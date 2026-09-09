"""Bounded in-memory rate limiting for gateway HTTP and WebSocket traffic."""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class SlidingWindowLimiter:
    """Thread-safe sliding-window limiter with bounded key cardinality."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("rate limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("rate-limit window must be > 0")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self._limit = limit
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitDecision:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    self._prune(cutoff)
                if len(self._hits) >= self._max_keys:
                    oldest_key = min(
                        self._hits,
                        key=lambda item: self._hits[item][-1] if self._hits[item] else cutoff,
                    )
                    self._hits.pop(oldest_key, None)
                hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, math.ceil(hits[0] + self._window - now))
                return RateLimitDecision(False, retry_after)
            hits.append(now)
            return RateLimitDecision(True)

    def _prune(self, cutoff: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)


def token_fingerprint(token: str) -> str:
    """Return a stable non-secret identity for rate-limit accounting."""
    return hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()


@dataclass(slots=True)
class GatewayRateLimiters:
    http_ip: SlidingWindowLimiter
    http_token: SlidingWindowLimiter
    ws_ip: SlidingWindowLimiter
    ws_token: SlidingWindowLimiter

    @classmethod
    def build(
        cls,
        *,
        http_limit: int,
        ws_limit: int,
        window_seconds: float,
    ) -> "GatewayRateLimiters":
        return cls(
            http_ip=SlidingWindowLimiter(http_limit, window_seconds),
            http_token=SlidingWindowLimiter(http_limit, window_seconds),
            ws_ip=SlidingWindowLimiter(ws_limit, window_seconds),
            ws_token=SlidingWindowLimiter(ws_limit, window_seconds),
        )
