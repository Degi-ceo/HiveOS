"""
orchestrator.py — the conversation loop (OpenJarvis QueryOrchestrator + Hermes
run_conversation). The brain that ties the subsystems into one turn.

Per turn (ADAPT, SYNTHESIS Part B; flow from HERMES_REFERENCE §3 + OPENJARVIS §4):
  restore-or-build the system prompt (prefix-cache byte-match via context) +
  prefetch memory recall -> build messages -> loop ≤ max_iterations:
    router.complete(messages, tools) ; if tool_calls -> dispatch each through the
    gate-routed ToolExecutor (loop-guarded), append tool results, continue ;
    else return the final answer. Post-turn: persist to the session store + memory.

All collaborators are injected and optional, so the loop is unit-testable with a
fake router and no network. Depends on core+llm+tools+context (+ duck-typed memory).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from hive.agents.base import AgentContext, AgentResult, ToolUsingAgent
from hive.agents.loop_guard import LoopGuard
from hive.context.compaction import compact
from hive.context.prompt_builder import (
    build_messages, restore_or_build_system_prompt, system_prompt,
)
from hive.core.events import EventBus, EventType
from hive.core.types import Message, Role
from hive.llm.router import ModelRouter
from hive.tools.base import BaseTool
from hive.tools.executor import DispatchStatus, ToolExecutor

log = logging.getLogger("hive.agents.orchestrator")


def _safe_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            log.warning("tool args not a dict (got %s), using {}", type(parsed).__name__)
            return {}
        return parsed
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("tool args JSON parse failed: %s | raw=%r", exc, raw[:200])
        return {}


class ConversationOrchestrator(ToolUsingAgent):
    agent_id = "orchestrator"

    def __init__(
        self,
        router: ModelRouter,
        *,
        tools: dict[str, BaseTool] | None = None,
        tool_executor: ToolExecutor | None = None,
        memory: Any = None,
        session_store: Any = None,
        max_iterations: int = 30,
        max_per_tool: int = 50,
        events: EventBus | None = None,
        summarizer: Callable[[list[Message], str], Awaitable[str]] | None = None,
        compact_trigger: int = 24,
    ) -> None:
        self._router = router
        self._tools = dict(tools or {})
        self._executor = tool_executor or (ToolExecutor(self._tools) if self._tools else None)
        self._memory = memory
        self._store = session_store
        self._max = max_iterations
        self._max_per_tool = max_per_tool
        self._events = events
        self._summarizer = summarizer
        self._compact_trigger = compact_trigger

    def _tool_schemas(self) -> list[dict] | None:
        # Hide unavailable tools from the model (B5): missing auth/config/context.
        usable = [t for t in self._tools.values() if t.available()]
        if not usable:
            return None
        return [{"name": t.spec.name, "description": t.spec.description,
                 "input_schema": t.spec.parameters or {"type": "object", "properties": {}}}
                for t in usable]

    async def run(self, input: str, context: AgentContext | None = None,
                  **kwargs: Any) -> AgentResult:
        return await self.ask(input, session_id=kwargs.get("session_id", "default"))

    async def ask(self, user_msg: str, *, session_id: str = "default") -> AgentResult:
        self._emit(EventType.AGENT_TURN_START, session=session_id)
        mem_block = self._memory.system_prompt_block() if self._memory else ""
        if self._store is not None:
            sys_prompt = restore_or_build_system_prompt(self._store, session_id, mem_block)
            history = self._store.messages(session_id, limit=40)
        else:
            sys_prompt, history = system_prompt(mem_block), []
        recall = self._memory.prefetch(user_msg, session_id=session_id) if self._memory else ""

        # Keep the prompt within budget: head/tail-protected compaction of long history.
        if self._summarizer is not None and len(history) > self._compact_trigger:
            history = await compact(history, summarizer=self._summarizer,
                                    trigger=self._compact_trigger)

        messages = build_messages(history, user_msg, recall_block=recall)
        schemas = self._tool_schemas()
        guard = LoopGuard(max_per_tool=self._max_per_tool)
        tool_results: list = []
        final = ""
        turns = 0

        for turns in range(1, self._max + 1):
            result = await self._router.complete(messages, system=sys_prompt, tools=schemas)
            if not result.tool_calls:
                final = result.text
                break
            messages.append(Message(role=Role.ASSISTANT, content=result.text,
                                    tool_calls=result.tool_calls))
            for call in result.tool_calls:
                args = _safe_args(call.arguments)
                reason = guard.check(call.name, args)
                if reason:
                    messages.append(Message(role=Role.TOOL, content=f"[loop-guard] {reason}",
                                            tool_call_id=call.id, name=call.name))
                    return self._finish(session_id, user_msg, f"Stopped: {reason}",
                                        tool_results, turns)
                content, result_obj = await self._dispatch(call.name, args)
                if result_obj is not None:
                    tool_results.append(result_obj)
                messages.append(Message(role=Role.TOOL, content=content,
                                        tool_call_id=call.id, name=call.name))
        else:
            final = final or "[max turns reached]"

        return self._finish(session_id, user_msg, final, tool_results, turns)

    async def _dispatch(self, name: str, args: dict[str, Any]):
        if self._executor is None:
            return f"[no executor available for {name}]", None
        dispatch = await self._executor.execute(name, args, reason="requested by Hive mid-turn")
        if dispatch.status is DispatchStatus.OK and dispatch.result is not None:
            return dispatch.result.content, dispatch.result
        if dispatch.status is DispatchStatus.PENDING:
            return f"[pending approval: {dispatch.approval_id}]", None
        return f"[tool error: {dispatch.error}]", None

    def _finish(self, session_id: str, user_msg: str, final: str,
                tool_results: list, turns: int) -> AgentResult:
        if self._store is not None:
            self._store.append(session_id, Role.USER, user_msg)
            self._store.append(session_id, Role.ASSISTANT, final)
        if self._memory is not None:
            self._memory.sync_turn(user_msg, final, session_id=session_id)
        self._emit(EventType.AGENT_TURN_END, session=session_id, turns=turns)
        return AgentResult(content=final, tool_results=tool_results, turns=turns)

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.publish(event_type, dict(data))
