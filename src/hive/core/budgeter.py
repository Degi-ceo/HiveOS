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
from typing import Callable, Mapping

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
        # Per-token cost telemetry (estimate; MiniMax credit billing is the hard gate).
        self._cost_today_usd = 0.0
        self._tokens_today = {"input": 0, "output": 0}
        self._by_model: dict[str, dict[str, float]] = {}

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._calls_today = today, 0
            self._cost_today_usd = 0.0
            self._tokens_today = {"input": 0, "output": 0}
            self._by_model = {}

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

    def record_usage(self, event: object = None) -> None:
        """Accrue per-token cost telemetry (wired to EventType.INFERENCE_END).

        Pure accumulator: the cost is computed upstream by the router (llm layer owns
        pricing) and arrives as `cost_usd` in the event, so core takes no llm import
        and stays a DAG leaf. Receives the EventBus Event ({model, input_tokens,
        output_tokens, cost_usd} in .data); a raw dict is accepted for direct calls.
        Cost is an estimate; the hard gate stays the call cap + polled credit window,
        so a wrong rate never blocks a turn."""
        raw = getattr(event, "data", event) or {}
        data = raw if isinstance(raw, Mapping) else {}
        model = str(data.get("model", "") or "unknown")
        inp = int(data.get("input_tokens", 0) or 0)
        out = int(data.get("output_tokens", 0) or 0)
        if inp == 0 and out == 0:
            return
        self._roll_day()
        cost = max(0.0, float(data.get("cost_usd", 0.0) or 0.0))
        self._cost_today_usd += cost
        self._tokens_today["input"] += inp
        self._tokens_today["output"] += out
        m = self._by_model.setdefault(model, {"input": 0, "output": 0, "cost_usd": 0.0})
        m["input"] += inp
        m["output"] += out
        m["cost_usd"] += cost

    def snapshot(self) -> dict:
        self._roll_day()
        remaining = None if self._used_pct is None else max(0.0, 100.0 - self._used_pct)
        return {"calls_today": self._calls_today, "daily_cap": self._daily_cap,
                "used_pct": self._used_pct, "remaining_pct": remaining,
                "cost_today_usd": round(self._cost_today_usd, 6),
                "tokens_today": dict(self._tokens_today),
                "by_model": self._by_model}

    def is_near_cap(self, *, threshold: float = 0.9) -> bool:
        """True when today's call count exceeds `threshold` fraction of the daily cap."""
        self._roll_day()
        return self._calls_today >= self._daily_cap * threshold

    def reset_daily(self) -> None:
        """Reset today's call count and cost telemetry to zero. For ops/test use."""
        self._calls_today = 0
        self._cost_today_usd = 0.0
        self._tokens_today = {"input": 0, "output": 0}
        self._by_model = {}

    def remaining_calls(self) -> int:
        """How many calls remain before the daily cap is hit."""
        self._roll_day()
        return max(0, self._daily_cap - self._calls_today)

    def forecast(self) -> dict:
        """Estimate capacity at the current call rate.

        Returns: calls_today, daily_cap, remaining_calls, pct_used, and a rough
        estimate of how many days of today's-rate usage remain before cap exhaustion.
        days_remaining is None when no calls have been made yet today."""
        self._roll_day()
        remaining = max(0, self._daily_cap - self._calls_today)
        pct_used = (self._calls_today / self._daily_cap * 100) if self._daily_cap else 0.0
        days_remaining: float | None = None
        if self._calls_today > 0:
            # Estimate: remaining quota / today's rate (today = 1 day's worth so far)
            days_remaining = round(remaining / self._calls_today, 2)
        return {
            "calls_today": self._calls_today,
            "daily_cap": self._daily_cap,
            "remaining_calls": remaining,
            "pct_used": round(pct_used, 2),
            "days_remaining": days_remaining,
            "cost_today_usd": round(self._cost_today_usd, 6),
        }

    def calls_per_hour(self) -> float:
        """Rolling call rate: calls_today divided by hours elapsed since midnight.

        Returns 0.0 if no calls today or the day just started (< 1 minute elapsed)."""
        self._roll_day()
        if self._calls_today == 0:
            return 0.0
        now = self._clock()
        midnight = now - (now % 86400)  # UTC midnight; close enough for rate estimate
        hours_elapsed = (now - midnight) / 3600.0
        if hours_elapsed < 1 / 60:  # less than 1 minute into the day
            return 0.0
        return round(self._calls_today / hours_elapsed, 4)

    def cost_per_call(self) -> float:
        """Average USD cost per call today. Returns 0.0 if no calls recorded."""
        self._roll_day()
        if self._calls_today == 0:
            return 0.0
        return round(self._cost_today_usd / self._calls_today, 8)

    def warning_status(self) -> dict | None:
        """Return a warning dict if budget health needs attention, else None.

        Triggers on: daily cap >= 80%, or MiniMax credit >= warn_pct. Returns None
        when everything is healthy so callers can do a simple truthiness check."""
        self._roll_day()
        pct_cap = (self._calls_today / self._daily_cap) if self._daily_cap else 0.0
        near_cap = pct_cap >= 0.8
        credit_warn = self._used_pct is not None and self._used_pct >= self._warn_pct
        if not near_cap and not credit_warn:
            return None
        now = self._clock()
        secs_since_midnight = now % 86400
        secs_til_reset = 86400.0 - secs_since_midnight
        return {
            "near_cap": near_cap,
            "pct_cap_used": round(pct_cap * 100, 2),
            "credit_pct_used": self._used_pct,
            "secs_til_reset": round(secs_til_reset, 1),
        }

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
