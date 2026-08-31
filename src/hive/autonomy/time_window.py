"""Fail-closed local-time execution window for autonomous heartbeat."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def check_window(spec: str, zone_name: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """Return whether an explicit HH:MM-HH:MM IANA-local window admits ``now``.

    Empty, invalid, or unavailable zone configuration always denies execution.
    A start equal to end is deliberately denied (never interpreted as 24 hours).
    """
    try:
        start_text, end_text = spec.split("-", 1)
        start = _minutes(start_text)
        end = _minutes(end_text)
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return False, "autonomy time window or IANA timezone is invalid or unavailable"
    if start == end:
        return False, "autonomy time window must have distinct start and end"
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    minute = current.hour * 60 + current.minute
    allowed = start <= minute < end if start < end else minute >= start or minute < end
    return (True, "") if allowed else (False, "outside configured autonomy time window")


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.strip().split(":", 1))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid local time")
    return hour * 60 + minute
