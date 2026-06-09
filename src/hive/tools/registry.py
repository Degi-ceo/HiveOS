"""
registry.py — audited tool registry on RegistryBase.

KEEP+ADAPT of Tools/registry.py (SYNTHESIS Part B): the old module mixed registry,
dispatch, audit, and builtins into one file. Here the registry is just the typed
store (RegistryBase[BaseTool]); dispatch/guardrails/audit move to tools/executor.py
and the builtins to tools/builtins/. Keyed by the tool's own spec.name.
"""
from __future__ import annotations

from hive.core.registry import RegistryBase
from hive.tools.base import BaseTool


class ToolRegistry(RegistryBase["BaseTool"]):
    @classmethod
    def add(cls, tool: BaseTool) -> BaseTool:
        cls.register_value(tool.spec.name, tool)
        return tool

    @classmethod
    def snapshot(cls) -> dict[str, BaseTool]:
        """A plain name->tool dict (what the executor dispatches over)."""
        return dict(cls.items())
