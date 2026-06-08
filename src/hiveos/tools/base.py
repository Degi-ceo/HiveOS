"""
base.py — tool descriptor + availability model (INTERFACE STUB).

Descriptor/planner/executor separation from OpenClaw (OPENCLAW_REFERENCE §8) +
BaseTool from OpenJarvis (OPENJARVIS_REFERENCE §5). Filled in Phase 5 of the build
plan (SYNTHESIS Part C); the existing Tools/registry.py keeps working until cutover.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from hiveos.core.types import ToolResult


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    category: str = "general"


class BaseTool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    async def execute(self, **params: Any) -> ToolResult: ...

    def to_openai_function(self) -> dict[str, Any]:
        s = self.spec
        return {"type": "function",
                "function": {"name": s.name, "description": s.description,
                             "parameters": s.parameters}}
