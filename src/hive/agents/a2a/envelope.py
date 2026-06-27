"""
envelope.py — minimal JSON-RPC-style envelope for HiveOS sub-agents (SPRINT_6 P-D).

Deliberately minimal: id, method, params, result, error. NOT full JSON-RPC 2.0
(no batch, no notifications). Future remote-agent bridge can extend if needed.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class A2AError(BaseModel):
    """JSON-RPC-shaped error object."""
    code: int
    message: str
    data: dict[str, Any] | None = None


class A2ARequest(BaseModel):
    """A2A request envelope."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class A2AResponse(BaseModel):
    """A2A response envelope. Exactly one of result or error must be set."""
    model_config = ConfigDict(extra="forbid")

    id: str
    result: Any | None = None
    error: A2AError | None = None

    def is_error(self) -> bool:
        return self.error is not None
