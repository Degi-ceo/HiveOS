"""
traces.py — per-session event traces off the EventBus (ADAPT OpenJarvis traces).

Groups bus events by session id so a turn can be inspected end to end. Bounded
ring per session. Subscribes only; never coupled to producers. Depends on core only.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

from hive.core.events import Event, EventBus, EventType

_TRACKED = (
    EventType.AGENT_TURN_START, EventType.AGENT_TURN_END,
    EventType.INFERENCE_END, EventType.TOOL_CALL_END,
    EventType.APPROVAL_REQUESTED,
    EventType.SELFMOD_START, EventType.SELFMOD_END,
    EventType.BUDGET_BLOCK,
)


class TraceCollector:
    def __init__(self, *, per_session_max: int = 200) -> None:
        self._traces: dict[str, Deque[Event]] = defaultdict(lambda: deque(maxlen=per_session_max))

    def attach(self, bus: EventBus) -> "TraceCollector":
        for event_type in _TRACKED:
            bus.subscribe(event_type, self._record)
        return self

    def _record(self, event: Event) -> None:
        session = str(event.data.get("session", "default"))
        self._traces[session].append(event)

    def trace(self, session: str = "default") -> list[Event]:
        return list(self._traces.get(session, ()))

    def sessions(self) -> list[str]:
        return list(self._traces)

    def export(self, session: str = "default") -> list[dict]:
        """Serializable trace for a session (JSON-friendly): one dict per event."""
        return [
            {"type": e.event_type.value, "ts": e.timestamp, "data": dict(e.data)}
            for e in self._traces.get(session, ())
        ]

    def export_all(self) -> dict[str, list[dict]]:
        return {session: self.export(session) for session in self._traces}

    def clear(self, session: str | None = None) -> int:
        """Clear traces for a specific session (or all sessions if None). Returns count cleared."""
        if session is not None:
            count = len(self._traces.get(session, ()))
            self._traces.pop(session, None)
            return count
        count = sum(len(q) for q in self._traces.values())
        self._traces.clear()
        return count

    def event_count(self, session: str = "default") -> int:
        """Return the number of events in a session's trace."""
        return len(self._traces.get(session, ()))

    def total_event_count(self) -> int:
        """Return the total number of events across all sessions."""
        return sum(len(q) for q in self._traces.values())

    def session_count(self) -> int:
        """Return the number of distinct sessions with at least one event."""
        return len(self._traces)

    def event_type_counts(self, session: str = "default") -> dict[str, int]:
        """Return a breakdown of events by type for a session."""
        counts: dict[str, int] = {}
        for e in self._traces.get(session, ()):
            key = e.event_type.value
            counts[key] = counts.get(key, 0) + 1
        return counts
