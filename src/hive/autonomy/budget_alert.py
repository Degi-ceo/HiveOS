"""
budget_alert.py — Telegram alert when budget forecast goes critical (SPRINT_7 Batch F).

Wraps a Budgeter + optional TelegramChannel and fires a single Telegram message
on each status transition (ok → warn → critical → exceeded → ok). No spam when
the status stays the same across ticks; the last-seen status is held in memory
on the Heartbeat instance so process restart resets the cooldown (intentional:
restart is rare and we want a fresh ping on the new run anyway).
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("hive.autonomy.budget_alert")


class _TelegramLike(Protocol):
    """Minimal interface the budget-alert needs from a Telegram transport."""

    async def send(self, message) -> object:  # pragma: no cover - structural
        ...


class BudgetAlert:
    """Send a Telegram alert when the spend forecast changes status.

    Args:
        hive: assembled HiveOS (config + budgeter).
        telegram: optional TelegramChannel-like transport. When None, the alert
            is computed and logged but never sent (useful in tests + when the
            gateway has no Telegram token configured).
        chat_id: target chat id. When empty, falls back to
            `hive.config.telegram_admin_chat_id` if present; otherwise sends are
            skipped (log only).
        threshold_days: only alert when days_until_cap <= threshold_days.
            Default 1 = alert the moment the cap is within a day.
    """

    def __init__(self, hive, *, telegram=None, chat_id: str = "",
                 threshold_days: int | None = None) -> None:
        self._hive = hive
        self._telegram = telegram
        self._chat_id = chat_id or getattr(hive.config, "telegram_admin_chat_id", "")
        if threshold_days is None:
            threshold_days = int(getattr(hive.config, "budget_forecast_alert_days", 1))
        self._threshold_days = max(0, threshold_days)
        self._last_status: str = "ok"

    @property
    def last_status(self) -> str:
        return self._last_status

    async def check(self) -> bool:
        """Evaluate the forecast and (maybe) send a Telegram alert.

        Returns True when an alert was sent on this tick.
        """
        forecast = self._hive.budgeter.forecast_spend(days=7)
        status = forecast.status
        # Transition detection: only fire when the status changes AND it's an
        # alert-worthy status. Re-pinging on every tick would be spam.
        transitioned = status != self._last_status
        alert_worthy = status in ("warn", "critical", "exceeded")
        if not (transitioned and alert_worthy):
            self._last_status = status
            return False
        # Threshold check: if the operator set a longer horizon, ignore shorter
        # statuses. (E.g. threshold=3 ignores "warn" with days_until_cap=2.)
        days = forecast.days_until_cap
        if days is not None and days > self._threshold_days:
            self._last_status = status
            return False
        msg = self._render(forecast)
        sent = await self._send(msg)
        if sent:
            log.info("budget alert sent: status=%s days_until_cap=%s", status, days)
        self._last_status = status
        return sent

    @staticmethod
    def _render(forecast) -> str:
        days = forecast.days_until_cap
        days_str = "now" if days == 0 else (f"{days} day(s)" if days is not None else "n/a")
        return (
            f"Budget forecast: {forecast.status.upper()}. "
            f"{days_str} until cap. "
            f"Projected: ${forecast.projected_total:.2f} "
            f"(avg ${forecast.daily_avg:.2f}/day, confidence {forecast.confidence:.2f})."
        )

    async def _send(self, text: str) -> bool:
        if self._telegram is None or not self._chat_id:
            log.info("budget alert (no telegram configured): %s", text)
            return False
        try:
            from hive.gateway.channels.base import OutgoingMessage
            msg = OutgoingMessage(chat_id=self._chat_id, text=text)
            result = await self._telegram.send(msg)
            # A boolean provider response is not enough to confirm an external
            # effect.  This alert has no durable outbox yet, so a missing receipt
            # is conservatively reported as not sent and never retried here.
            ok = bool(getattr(result, "ok", False) and getattr(result, "message_id", ""))
            if not ok:
                log.warning("budget alert send returned not-ok: %s",
                            getattr(result, "error", "unknown"))
            return ok
        except Exception as exc:  # noqa: BLE001 - alerting must not crash the tick
            log.warning("budget alert send failed: %s", exc)
            return False


def make_budget_alert(hive) -> BudgetAlert:
    """Construct a BudgetAlert from the runtime config (lazy TelegramChannel).

    Returns a no-network alert (log only) when TELEGRAM_BOT_TOKEN is unset.
    """
    token = getattr(hive.config, "telegram_token", "")
    channel = None
    if token:
        try:
            from hive.gateway.channels.telegram import TelegramChannel
            channel = TelegramChannel(token)
        except Exception as exc:  # noqa: BLE001 - importing telegram is best-effort
            log.debug("telegram channel unavailable for budget alert: %s", exc)
    return BudgetAlert(hive, telegram=channel)
