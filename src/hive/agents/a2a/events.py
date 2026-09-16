"""
events.py — A2A event-emission helpers (SPRINT_6 P-G, issue #75).

Pure helpers that publish A2A lifecycle events onto the global EventBus.
No state lives here — consumers (BoardStore, /ws/dashboard, the React
Kanban via WS) subscribe to EventType.A2A_CALL_{STARTED,COMPLETED,FAILED}.
"""
from __future__ import annotations

from typing import Any

from hive.core.events import EventBus, EventType


def emit_call_started(
    bus: EventBus, *, method: str, request_id: str, agent_name: str,
    task: str, session_id: str | None = None,
) -> None:
    """Publish metadata-only start state; task/session text never reaches the bus."""
    bus.publish(
        EventType.A2A_CALL_STARTED,
        {
            "method": method,
            "request_id": request_id,
            "agent_name": agent_name,
        },
    )


def emit_call_completed(
    bus: EventBus, *, method: str, request_id: str, agent_name: str, result: Any,
) -> None:
    """Publish completion metadata; delegated output remains private."""
    bus.publish(
        EventType.A2A_CALL_COMPLETED,
        {
            "method": method,
            "request_id": request_id,
            "agent_name": agent_name,
        },
    )


def emit_call_failed(
    bus: EventBus, *, method: str, request_id: str, agent_name: str, error: str,
) -> None:
    """Publish failure metadata; raw exception text can contain secrets."""
    bus.publish(
        EventType.A2A_CALL_FAILED,
        {
            "method": method,
            "request_id": request_id,
            "agent_name": agent_name,
        },
    )
