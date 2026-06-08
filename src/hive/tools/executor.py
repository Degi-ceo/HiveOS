"""
executor.py — tool dispatch with approval-gate guardrails + audit.

ADAPT of OpenJarvis ToolExecutor + Hermes tool_executor/file_safety (SYNTHESIS
Part B). One choke point for every tool call:

  unknown -> ERROR
  dangerous (spec flag OR gate.is_dangerous) -> route to the PROTECTED gate,
      return PENDING with an approval id, DO NOT execute
  otherwise -> execute, capturing failures as ERROR

The danger firewall is the canonical Core/approval_gate.py (via core.approval) —
never re-implemented here. Audit + EventBus are injected so the storage sink
(observability, P8) stays decoupled. A closed ToolDispatch makes the three
outcomes unrepresentable-as-anything-else (OpenClaw: make impossible states
unrepresentable).
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hive.core import approval
from hive.core.events import EventBus, EventType
from hive.core.types import ToolResult
from hive.tools.base import BaseTool

log = logging.getLogger("hive.tools.executor")

AuditSink = Callable[[dict[str, Any]], None]


class DispatchStatus(str, enum.Enum):
    OK = "ok"
    PENDING = "pending_approval"
    ERROR = "error"


@dataclass(slots=True)
class ToolDispatch:
    status: DispatchStatus
    result: ToolResult | None = None
    approval_id: str | None = None
    error: str | None = None


class ToolExecutor:
    def __init__(
        self,
        tools: Mapping[str, BaseTool],
        *,
        gate: Any = None,
        audit: AuditSink | None = None,
        events: EventBus | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._gate = gate or approval.gate
        self._audit = audit
        self._events = events

    async def execute(self, name: str, args: dict[str, Any] | None = None,
                      *, reason: str = "") -> ToolDispatch:
        args = dict(args or {})
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"unknown tool: {name}"))

        if tool.spec.dangerous or self._gate.is_dangerous(name, args):
            approval_id = str(self._gate.request(name, args, reason))
            self._emit(EventType.APPROVAL_REQUESTED, tool=name, approval_id=approval_id)
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.PENDING, approval_id=approval_id))

        return self._finish(name, args, await self._run(tool, args))

    async def execute_approved(self, name: str, args: dict[str, Any]) -> ToolDispatch:
        """Run a previously gated tool after the human approved it."""
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"unknown tool: {name}"))
        return self._finish(name, args, await self._run(tool, args), approved=True)

    async def _run(self, tool: BaseTool, args: dict[str, Any]) -> ToolDispatch:
        try:
            result = await tool.execute(**args)
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            log.warning("tool %s failed: %s", tool.spec.name, exc)
            return ToolDispatch(DispatchStatus.ERROR, error=str(exc))
        return ToolDispatch(DispatchStatus.OK, result=result)

    def _finish(self, name: str, args: dict[str, Any], dispatch: ToolDispatch,
                *, approved: bool = False) -> ToolDispatch:
        if self._audit is not None:
            self._audit({"tool": name, "args": args, "status": dispatch.status.value,
                         "approved": approved, "error": dispatch.error})
        if dispatch.status is not DispatchStatus.PENDING:
            self._emit(EventType.TOOL_CALL_END, tool=name, status=dispatch.status.value)
        return dispatch

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.publish(event_type, dict(data))
