"""
protocol.py — typed gateway request/response models (PATTERN, OpenClaw
gateway-protocol). Pydantic at the HTTP boundary; the core speaks its own types.
"""
from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class ApprovalDecision(BaseModel):
    approval_id: str
    approved: bool
