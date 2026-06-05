"""
budgeter.py — self-calibrating rate/credit guard for the MiniMax Token Plan.

The plan is credit-based with rolling 5h + weekly windows, NOT a fixed call
count. So we poll the official remains endpoint and self-calibrate, plus keep a
local daily call counter and a hard cap. Every model call checks can_spend().
"""
from __future__ import annotations
import time
import logging
import httpx

from core import settings

log = logging.getLogger("hiveos.budgeter")


class Budgeter:
    def __init__(self) -> None:
        self._day = time.strftime("%Y-%m-%d")
        self._calls_today = 0
        self._last_remains_pct: float | None = None
        self._last_poll = 0.0

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._calls_today = today, 0

    async def remaining_pct(self) -> float | None:
        """Poll MiniMax remains endpoint (cached 60s). Returns remaining %."""
        if time.time() - self._last_poll < 60 and self._last_remains_pct is not None:
            return self._last_remains_pct
        if not settings.MINIMAX_API_KEY:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    settings.REMAINS_URL,
                    headers={"Authorization": f"Bearer {settings.MINIMAX_API_KEY}"},
                )
                data = r.json()
                pct = data.get("usage_percent", data.get("usagePercent"))
                self._last_remains_pct = float(pct) if pct is not None else None
                self._last_poll = time.time()
                if self._last_remains_pct is not None and \
                        self._last_remains_pct <= (100 - settings.WINDOW_WARN_PCT):
                    log.warning("MiniMax window low: %.0f%% remaining", self._last_remains_pct)
                return self._last_remains_pct
        except Exception as e:  # noqa: BLE001
            log.debug("remains poll failed: %s", e)
            return None

    async def can_spend(self) -> tuple[bool, str]:
        self._roll_day()
        if self._calls_today >= settings.DAILY_CALL_CAP:
            return False, f"daily cap reached ({settings.DAILY_CALL_CAP})"
        pct = await self.remaining_pct()
        if pct is not None and pct <= 2:
            return False, "MiniMax window nearly exhausted"
        return True, "ok"

    def record_call(self) -> None:
        self._roll_day()
        self._calls_today += 1


budgeter = Budgeter()
