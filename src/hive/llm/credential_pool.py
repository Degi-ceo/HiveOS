"""
credential_pool.py — multi-key failover pool with cooldowns.

Ported from Hermes credential_pool.py (docs/references/HERMES_REFERENCE.md §4):
several API keys behind one acquire()/report_* interface. A key that hits a
rotate-worthy failure (auth/billing/rate-limit) is parked on a cooldown so the
router moves to the next key instead of hammering a dead one. `clock` is injectable
for deterministic tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass(slots=True)
class PooledCredential:
    key: str
    label: str = ""
    failures: int = 0
    cooldown_until: float = 0.0


class CredentialPool:
    """Round-robin pool that skips credentials in cooldown."""

    def __init__(
        self,
        keys: list[str] | None = None,
        *,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Preserve order; drop empties/dupes (a blank MINIMAX_API_KEY is "no creds").
        seen: set[str] = set()
        self._creds: list[PooledCredential] = []
        for k in keys or []:
            if k and k not in seen:
                seen.add(k)
                self._creds.append(PooledCredential(key=k, label=f"key{len(self._creds)}"))
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._creds)

    def available(self) -> list[PooledCredential]:
        now = self._clock()
        return [c for c in self._creds if c.cooldown_until <= now]

    def acquire(self) -> PooledCredential | None:
        """Next non-cooled credential (round-robin), or None if all are cooling."""
        n = len(self._creds)
        if n == 0:
            return None
        now = self._clock()
        for i in range(n):
            cred = self._creds[(self._cursor + i) % n]
            if cred.cooldown_until <= now:
                self._cursor = (self._cursor + i + 1) % n
                return cred
        return None

    def report_success(self, cred: PooledCredential) -> None:
        cred.failures = 0
        cred.cooldown_until = 0.0

    def report_failure(self, cred: PooledCredential, *, cooldown: float | None = None) -> None:
        cred.failures += 1
        cred.cooldown_until = self._clock() + (self._cooldown if cooldown is None else cooldown)
