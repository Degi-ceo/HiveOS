"""
budgeter.py — credit/rate guard for the MiniMax Token Plan (KEEP+ADAPT).

Ported from Core/budgeter.py. Two layers: a hard local daily call cap, and the
plan's rolling credit window polled from MiniMax's remains endpoint. The router's
budget check must be synchronous, so `gate()` reads only cached state (cap +
last-polled pct); `refresh()` does the network poll out-of-band (heartbeat).
`record_call()` is wired to INFERENCE_END so every successful call counts.

Lives in core (leaf): depends on stdlib + httpx only, never a higher layer.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

log = logging.getLogger("hive.budgeter")


class Budgeter:
    def __init__(self, *, daily_cap: int = 3000, warn_pct: float = 70.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._daily_cap = daily_cap
        self._warn_pct = warn_pct
        self._clock = clock
        self._day = self._today()
        self._calls_today = 0
        # Percent of the credit window CONSUMED (the remains endpoint's `usage_percent`).
        self._used_pct: float | None = None

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._calls_today = today, 0

    def gate(self) -> tuple[bool, str]:
        """Synchronous check for the router. Reads cached state only."""
        self._roll_day()
        if self._calls_today >= self._daily_cap:
            return False, f"daily cap reached ({self._daily_cap})"
        if self._used_pct is not None and self._used_pct >= 98:
            return False, "MiniMax credit window nearly exhausted"
        return True, ""

    def record_call(self, *_args: object) -> None:
        """Count a successful call (wired to EventType.INFERENCE_END)."""
        self._roll_day()
        self._calls_today += 1

    def snapshot(self) -> dict:
        self._roll_day()
        remaining = None if self._used_pct is None else max(0.0, 100.0 - self._used_pct)
        return {"calls_today": self._calls_today, "daily_cap": self._daily_cap,
                "used_pct": self._used_pct, "remaining_pct": remaining}

    async def refresh(self, api_key: str, remains_url: str) -> float | None:
        """Poll the remains endpoint; cache % CONSUMED. Best-effort. Returns used %.

        NOTE: the endpoint's field is `usage_percent` (consumed), so gate() blocks when
        used >= 98 and warn fires at >= warn_pct. Confirm the field meaning against the
        live endpoint if budgeting ever looks off.
        """
        if not api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(remains_url, headers={"Authorization": f"Bearer {api_key}"})
                data = r.json()
            pct = data.get("usage_percent", data.get("usagePercent"))
            self._used_pct = float(pct) if pct is not None else None
            if self._used_pct is not None and self._used_pct >= self._warn_pct:
                log.warning("MiniMax credit window: %.0f%% consumed", self._used_pct)
            return self._used_pct
        except Exception as exc:  # noqa: BLE001 - polling is best-effort
            log.debug("remains poll failed: %s", exc)
            return None
