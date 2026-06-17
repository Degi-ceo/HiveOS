"""
base.py — BaseTool ABC + ToolSpec (implemented by builtins, MCPTool).

Descriptor/planner/executor separation from OpenClaw (OPENCLAW_REFERENCE §8) +
BaseTool from OpenJarvis (OPENJARVIS_REFERENCE §5). Filled in Phase 5 of the build
plan (SYNTHESIS Part C); the existing Tools/registry.py keeps working until cutover.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from hive.core.types import ToolResult


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    category: str = "general"

    def __repr__(self) -> str:
        return f"ToolSpec(name={self.name!r}, category={self.category!r}, dangerous={self.dangerous})"


class BaseTool(ABC):
    spec: ToolSpec

    @abstractmethod
    async def execute(self, **params: Any) -> ToolResult: ...

    def available(self) -> bool:
        """Whether this tool is usable right now (auth/config/context present).
        Default True; override for tools needing a key/service. Unavailable tools are
        hidden from the model (orchestrator) and refused by the executor (OpenClaw #8)."""
        return True

    def to_openai_function(self) -> dict[str, Any]:
        s = self.spec
        return {"type": "function",
                "function": {"name": s.name, "description": s.description,
                             "parameters": s.parameters}}
