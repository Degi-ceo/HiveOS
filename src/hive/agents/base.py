"""
base.py — agent base classes (implemented by ConversationOrchestrator, delegate, executor).

Ported contract from OpenJarvis agents/_stubs.py (OPENJARVIS_REFERENCE §3.4):
BaseAgent.run() is the single entry point; ToolUsingAgent adds tools + loop guard.
Filled in Phase 6 of the build plan (SYNTHESIS Part C).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hive.core.types import Conversation, ToolResult


@dataclass(slots=True)
class AgentContext:
    conversation: Conversation = field(default_factory=Conversation)
    tools: list[str] = field(default_factory=list)
    memory_results: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TerminalOutcome(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    LOOP_GUARD = "loop_guard"
    TOOL_ERROR = "tool_error"


@dataclass(slots=True)
class AgentResult:
    content: str
    tool_results: list[ToolResult] = field(default_factory=list)
    turns: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome: TerminalOutcome = field(default=TerminalOutcome.COMPLETED)


class BaseAgent(ABC):
    agent_id: str = "base"
    accepts_tools: bool = False

    @abstractmethod
    async def run(self, input: str, context: AgentContext | None = None,
                  **kwargs: Any) -> AgentResult: ...


class ToolUsingAgent(BaseAgent):
    accepts_tools = True
