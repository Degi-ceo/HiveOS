"""test_a2a_events.py — SPRINT_6 P-G A2A event-type + emit-helper coverage (issue #75)."""
from __future__ import annotations

from hive.agents.a2a.events import (
    emit_call_completed,
    emit_call_failed,
    emit_call_started,
)
from hive.core.events import Event, EventBus, EventType


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


def _collect(bus: EventBus, et: EventType) -> list[Event]:
    out: list[Event] = []
    bus.subscribe(et, out.append)
    return out


def test_emit_call_started_publishes_with_full_payload():
    bus = EventBus()
    seen = _collect(bus, EventType.A2A_CALL_STARTED)
    emit_call_started(bus, method="researcher.run", request_id="req1",
                      agent_name="researcher", task="find x")
    assert len(seen) == 1
    ev = seen[0]
    assert ev.event_type is EventType.A2A_CALL_STARTED
    assert ev.data == {
        "method": "researcher.run",
        "request_id": "req1",
        "agent_name": "researcher",
        "task": "find x",
        "session_id": None,
    }


def test_emit_call_started_includes_session_id_when_provided():
    bus = EventBus()
    seen = _collect(bus, EventType.A2A_CALL_STARTED)
    emit_call_started(bus, method="coder.run", request_id="r2",
                      agent_name="coder", task="refactor x",
                      session_id="sess-abc")
    assert seen[0].data["session_id"] == "sess-abc"


def test_emit_call_completed_publishes_with_result():
    bus = EventBus()
    seen = _collect(bus, EventType.A2A_CALL_COMPLETED)
    emit_call_completed(bus, method="coder.run", request_id="r3",
                        agent_name="coder", result="done")
    assert len(seen) == 1
    assert seen[0].event_type is EventType.A2A_CALL_COMPLETED
    assert seen[0].data == {
        "method": "coder.run", "request_id": "r3",
        "agent_name": "coder", "result": "done",
    }


def test_emit_call_failed_publishes_with_error_message():
    bus = EventBus()
    seen = _collect(bus, EventType.A2A_CALL_FAILED)
    emit_call_failed(bus, method="reviewer.run", request_id="r4",
                     agent_name="reviewer", error="kaboom")
    assert len(seen) == 1
    assert seen[0].event_type is EventType.A2A_CALL_FAILED
    assert seen[0].data == {
        "method": "reviewer.run", "request_id": "r4",
        "agent_name": "reviewer", "error": "kaboom",
    }
