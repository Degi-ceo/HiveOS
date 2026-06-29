"""test_a2a_events.py — SPRINT_6 P-G A2A event-type + emit-helper coverage (issue #75)."""
from __future__ import annotations

from hive.core.events import EventType


def test_a2a_event_type_values_are_exact_strings():
    assert EventType.A2A_CALL_STARTED.value == "a2a.call.started"
    assert EventType.A2A_CALL_COMPLETED.value == "a2a.call.completed"
    assert EventType.A2A_CALL_FAILED.value == "a2a.call.failed"


def test_a2a_event_types_are_distinct():
    types_ = {
        EventType.A2A_CALL_STARTED,
        EventType.A2A_CALL_COMPLETED,
        EventType.A2A_CALL_FAILED,
    }
    assert len(types_) == 3