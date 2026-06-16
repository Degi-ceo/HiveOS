"""
protocol.py — typed gateway request/response models (PATTERN, OpenClaw
gateway-protocol). Pydantic at the HTTP boundary; the core speaks its own types.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# Gateway protocol version. Bump MINOR for additive changes (new optional fields/
# endpoints), MAJOR only for breaking changes — clients read it from /health and every
# ChatResponse. Additive-first is the contract (OpenClaw gateway-protocol).
PROTOCOL_VERSION = "1.0"


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    protocol_version: str = Field(default=PROTOCOL_VERSION)
    request_id: str | None = None


class ApprovalDecision(BaseModel):
    approval_id: str
    approved: bool
