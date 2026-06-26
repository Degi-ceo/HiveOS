"""
types.py — canonical dataclasses shared across HiveOS.

Single source of truth for the chat protocol and tool contracts (ported from
OpenJarvis core/types.py, see docs/references/OPENJARVIS_REFERENCE.md §3.1).
Provider adapters normalize to/from these at the edge.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Role(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON-encoded string (OpenAI-compatible)


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass(slots=True)
class Conversation:
    """Sliding-window message list (cap optional)."""
    messages: list[Message] = field(default_factory=list)
    max_messages: int | None = None

    def add(self, message: Message) -> None:
        self.messages.append(message)
        if self.max_messages is not None and len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]


@dataclass(slots=True)
class ToolResult:
    tool_name: str
    content: str
    success: bool = True
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.success


@dataclass(slots=True)
class ModelSpec:
    model_id: str
    name: str = ""
    context_length: int = 0
    provider: str = ""
    requires_api_key: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class StepType(str, enum.Enum):
    ROUTE = "route"
    RETRIEVE = "retrieve"
    GENERATE = "generate"
    TOOL_CALL = "tool_call"
    RESPOND = "respond"


# --- Learning loop (SPRINT_6 P-F) ------------------------------------------------
#
# The learning loop persists two kinds of rows:
#   * TraceRow    — one observation per tool-call (tracer writes)
#   * LoopOutcome — one row per learning-loop run (loop writes verdict)
#
# Verdict constants are str so they survive SQLite round-tripping without
# coercion. ``LoopOutcome.verdict`` is the source of truth for accept/reject.

VERDICT_ACCEPT = "accept"
VERDICT_REJECT = "reject"


@dataclass(slots=True)
class TraceRow:
    """One tracer observation — survives a JSON-blobbed ``args`` payload."""
    id: int = 0
    ts: float = 0.0
    session_id: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    outcome: str = "ok"   # "ok" | "error" | "denied"
    latency_ms: float = 0.0
    error_class: str | None = None
    error_message: str | None = None  # already redacted by audit emit


@dataclass(slots=True)
class LoopOutcome:
    """One learning-loop run result."""
    id: int = 0
    ts: float = 0.0
    symptom: str = ""
    verdict: str = VERDICT_REJECT
    pytest_baseline: float = 0.0
    pytest_candidate: float = 0.0
    evals_baseline: float = 0.0
    evals_candidate: float = 0.0
    worktree_branch: str | None = None
    pr_url: str | None = None
    reject_reason: str | None = None
