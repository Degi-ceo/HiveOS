"""Typed, line-delimited IPC messages for supervised local specialists.

The worker protocol intentionally has a very small vocabulary.  A worker can
ask its supervisor to perform model inference or a profile-scoped tool call;
it never receives an executor credential, approval credential, or a live tool
object.  The transport is local stdio, not a network API.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_000_000


class WorkerProtocolError(ValueError):
    """Raised for malformed or unsupported worker messages."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    """A supervisor-owned request to a closed specialist role."""

    role: str
    task: str
    run_id: str
    delegation_id: str = ""
    capability_id: str = ""
    request_id: str = field(default_factory=lambda: str(uuid4()))
    max_iterations: int = 30
    max_per_tool: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": PROTOCOL_VERSION,
            "type": "start",
            "request_id": self.request_id,
            "role": self.role,
            "task": self.task,
            "run_id": self.run_id,
            "delegation_id": self.delegation_id,
            "capability_id": self.capability_id,
            "max_iterations": self.max_iterations,
            "max_per_tool": self.max_per_tool,
        }


def encode(message: dict[str, Any]) -> bytes:
    """Encode one bounded JSON frame; stdout must contain protocol only."""
    try:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("message is not JSON serializable") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("worker message exceeds maximum frame size")
    return encoded + b"\n"


def decode(raw: bytes | str) -> dict[str, Any]:
    """Decode and structurally validate one protocol frame."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not data or len(data) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("invalid worker frame size")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("invalid worker JSON frame") from exc
    if not isinstance(value, dict) or value.get("version") != PROTOCOL_VERSION:
        raise WorkerProtocolError("unsupported worker protocol message")
    if not isinstance(value.get("type"), str):
        raise WorkerProtocolError("worker message has no type")
    return value
