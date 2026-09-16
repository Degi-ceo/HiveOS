"""Safe public envelopes for live operator event streams.

The agent loop needs rich internal events to execute tools, while terminal and
gateway observers need progress information without receiving model reasoning,
tool arguments, or raw tool output. This module makes that boundary explicit.
"""
from __future__ import annotations

import time
from typing import Any

from hive.core.redact import redact_known_secrets

_PUBLIC_EVENT_TYPES = frozenset({
    "model_decision", "tool_call_start", "tool_call_end", "final", "max_turns",
    "loop_guard", "error", "subagent_start", "subagent_end", "operator_action",
    "specialist_lifecycle", "candidate_check",
})
_SPECIALIST_STATES = frozenset({
    "queued", "running", "review_required", "completed", "failed", "cancelled", "interrupted",
})
_CHECK_STATES = frozenset({"started", "passed", "failed", "cancelled"})


def _choice(value: object, allowed: frozenset[str], fallback: str) -> str:
    """Return a bounded vocabulary value, never a caller-controlled label."""
    candidate = str(value).casefold()
    return candidate if candidate in allowed else fallback


def _bounded_int(value: object, *, minimum: int = 0, maximum: int = 1000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(parsed, maximum))


def public_operator_event(
    event: dict[str, Any], *, run_id: str, session_id: str, sequence: int,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a versioned, redacted event suitable for an operator surface."""
    event_type = str(event.get("type") or "status")
    if event_type not in _PUBLIC_EVENT_TYPES:
        event_type = "status"
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
            status = str(event.get("status", "finished"))
            result["status"] = status
            # This intentionally says only how the operation ended. Copying a
            # model/tool result here would make the operator stream a second,
            # less-controlled transcript sink.
            result["summary"] = {
                "ok": "completed",
                "approved": "completed",
                "pending": "awaiting approval",
            }.get(status.casefold(), "failed" if status.casefold() in {"error", "failed"} else "finished")
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
    elif event_type == "operator_action":
        # Terminal control actions are intentionally a small public envelope:
        # no raw task payload, model reasoning, or credential material belongs
        # in a durable operator replay stream.
        result.update({
            "name": str(event.get("name", "operator action"))[:96],
            "status": str(event.get("status", "completed"))[:32],
            "summary": redact_known_secrets(str(event.get("summary", "")))[:500],
        })
    elif event_type == "specialist_lifecycle":
        # A delegation is deliberately observable only as a role/state machine.
        # Its task, result, and worker transcript stay out of the public stream.
        result.update({
            "id": str(event.get("id", ""))[:128],
            "agent": str(event.get("agent", "specialist"))[:64],
            "status": _choice(event.get("status"), _SPECIALIST_STATES, "failed"),
            "attempt": _bounded_int(event.get("attempt")),
        })
    elif event_type == "candidate_check":
        # ``check_kind`` is computed from the fixed argv allowlist; the raw
        # argv, candidate path, image and command output are never accepted.
        result.update({
            "edit_id": str(event.get("edit_id", ""))[:128],
            "delegation_id": str(event.get("delegation_id", ""))[:128],
            "check_kind": _choice(event.get("check_kind"), frozenset({"pytest", "compileall", "ruff"}), "pytest"),
            "status": _choice(event.get("status"), _CHECK_STATES, "failed"),
        })
    return result
