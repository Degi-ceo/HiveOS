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
