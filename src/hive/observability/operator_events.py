"""Safe public envelopes for live operator event streams.

The agent loop needs rich internal events to execute tools, while terminal and
gateway observers need progress information without receiving model reasoning,
tool arguments, or raw tool output. This module makes that boundary explicit.
"""
from __future__ import annotations

import time
from typing import Any

from hive.core.redact import redact_known_secrets


def public_operator_event(
    event: dict[str, Any], *, run_id: str, session_id: str, sequence: int,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a versioned, redacted event suitable for an operator surface."""
    event_type = str(event.get("type") or "status")
    result: dict[str, Any] = {
        "version": 1,
        "type": event_type,
        "run_id": str(run_id),
        "session_id": str(session_id),
        "sequence": int(sequence),
        "timestamp": float(time.time() if timestamp is None else timestamp),
    }
    if event_type == "model_decision":
        calls = event.get("tool_calls") or []
        result["turn"] = int(event.get("turn", 0))
        result["tool_calls"] = [
            {"id": str(call.get("id", "")), "name": str(call.get("name", "tool"))}
            for call in calls if isinstance(call, dict)
        ]
    elif event_type in {"tool_call_start", "tool_call_end"}:
        result.update({
            "turn": int(event.get("turn", 0)),
            "id": str(event.get("id", "")),
            "name": str(event.get("name", "tool")),
        })
        if event_type == "tool_call_end":
            result["status"] = str(event.get("status", "finished"))
    elif event_type in {"final", "max_turns"}:
        result["turn"] = int(event.get("turn", 0))
        result["text"] = redact_known_secrets(str(event.get("text", "")))
        result["tool_calls"] = int(event.get("tool_calls", 0))
    elif event_type == "loop_guard":
        result.update({
            "turn": int(event.get("turn", 0)),
            "name": str(event.get("tool", "tool")),
            "reason": redact_known_secrets(str(event.get("reason", "safety stop")))[:500],
        })
    elif event_type == "error":
        result["class"] = str(event.get("class", "RuntimeError"))[:120]
    elif event_type in {"subagent_start", "subagent_end"}:
        result.update({
            "turn": int(event.get("turn", 0)),
            "id": str(event.get("id", "")),
            "agent": str(event.get("agent", "specialist"))[:64],
        })
        if event_type == "subagent_end":
            result["status"] = str(event.get("status", "finished"))
    return result
