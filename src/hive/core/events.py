"""
events.py — thread-safe pub/sub EventBus (the connective tissue).

Ported from OpenJarvis's EventBus (docs/references/OPENJARVIS_REFERENCE.md §3.1).
Telemetry, traces, audit, and the budgeter subscribe here rather than coupling to
producers. Synchronous dispatch: subscribers run in registration order on the
publishing thread.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("hive.events")


class EventType(enum.Enum):
    # Inference
    INFERENCE_START = "inference_start"
    INFERENCE_END = "inference_end"
    # Tools
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    # Memory
    MEMORY_STORE = "memory_store"
    MEMORY_RETRIEVE = "memory_retrieve"
    # Agent lifecycle
    AGENT_TURN_START = "agent_turn_start"
    AGENT_TURN_END = "agent_turn_end"
    AGENT_TICK_START = "agent_tick_start"
    AGENT_TICK_END = "agent_tick_end"
    # Approvals / security
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    # Telemetry / budget
    TELEMETRY_RECORD = "telemetry_record"
    BUDGET_BLOCK = "budget_block"
    # Self-modification
    SELFMOD_START = "selfmod_start"
    SELFMOD_END = "selfmod_end"
    # A2A sub-agent calls (SPRINT_6 P-G, issue #75)
    A2A_CALL_STARTED = "a2a.call.started"
    A2A_CALL_COMPLETED = "a2a.call.completed"
    A2A_CALL_FAILED = "a2a.call.failed"


@dataclass(slots=True)
class Event:
    event_type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


Subscriber = Callable[[Event], None]


class EventBus:
    """Thread-safe synchronous pub/sub with optional bounded history."""

    def __init__(self, *, record_history: bool = False, history_max: int = 1000) -> None:
        self._subs: dict[EventType, list[Subscriber]] = {}
        self._lock = threading.Lock()
        self._record_history = record_history
        self._history_max = history_max
        self._history: list[Event] = []

    def subscribe(self, event_type: EventType, callback: Subscriber) -> None:
        with self._lock:
            self._subs.setdefault(event_type, []).append(callback)

    def publish(self, event_type: EventType, data: dict[str, Any] | None = None) -> None:
        event = Event(event_type, data or {})
        with self._lock:
            subs = list(self._subs.get(event_type, ()))
            if self._record_history:
                self._history.append(event)
                if len(self._history) > self._history_max:
                    self._history = self._history[-self._history_max:]
        for cb in subs:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001 - one bad subscriber must not break others
                log.warning("event subscriber error for %s: %s", event_type, exc)

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)

    def subscribe_once(self, event_type: EventType, callback: Subscriber) -> None:
        """Subscribe a callback that fires exactly once then auto-unregisters."""
        def _wrapper(event: Event) -> None:
            try:
                callback(event)
            finally:
                with self._lock:
                    try:
                        self._subs.get(event_type, []).remove(_wrapper)
                    except ValueError:
                        pass  # already removed (race condition)
        self.subscribe(event_type, _wrapper)

    def history_count(self) -> int:
        """Return the number of events currently held in the rolling history."""
        with self._lock:
            return len(self._history)

    def clear_history(self) -> int:
        """Clear the event history and return the count cleared. Useful for tests."""
        with self._lock:
            count = len(self._history)
            self._history = []
            return count

    def unsubscribe_all(self, event_type: EventType) -> int:
        """Remove all subscribers for an event type. Returns the count removed."""
        with self._lock:
            subs = self._subs.pop(event_type, [])
            return len(subs)

    def subscriber_count(self, event_type: EventType) -> int:
        """Return the number of active subscribers for an event type."""
        with self._lock:
            return len(self._subs.get(event_type, []))

    def total_subscribers(self) -> int:
        """Return the total number of subscriber registrations across all event types."""
        with self._lock:
            return sum(len(v) for v in self._subs.values())

    def history_by_type(self) -> dict[str, int]:
        """Return counts of each event type in the rolling history (requires record_history=True)."""
        with self._lock:
            counts: dict[str, int] = {}
            for ev in self._history:
                key = ev.event_type.value
                counts[key] = counts.get(key, 0) + 1
            return counts

    def recent_events(self, n: int = 20) -> list[dict]:
        """Return the n most recent events as JSON-serialisable dicts (newest first)."""
        with self._lock:
            tail = list(self._history[-n:])
        tail.reverse()
        return [{"event_type": ev.event_type.value, "data": ev.data, "ts": ev.timestamp}
                for ev in tail]


_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    """Process-wide default bus."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
