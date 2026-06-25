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

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from hive.agents.base import AgentContext, AgentResult, TerminalOutcome, ToolUsingAgent
from hive.agents.loop_guard import LoopGuard
from hive.context.compaction import compact
from hive.context.prompt_builder import (
    build_messages,
    restore_or_build_system_prompt,
    system_prompt,
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
        planner: Any = None,
        goals: list[str] | None = None,
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
        self._planner = planner
        self._goals = list(goals or [])

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
        return await self.ask(input, session_id=kwargs.get("session_id", "default"),
                              channel_hint=kwargs.get("channel_hint", ""))

    async def ask(self, user_msg: str, *, session_id: str = "default",
                  channel_hint: str = "") -> AgentResult:
        """Run one turn and return the final AgentResult. Wraps _run_loop
        with sink=None (no streaming). The streaming variant is stream_ask()."""
        return await self._run_loop(user_msg, session_id=session_id,
                                    channel_hint=channel_hint, sink=None)

    async def stream_ask(
        self, user_msg: str, *, session_id: str = "default",
        channel_hint: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn and yield per-iteration events as they happen.

        Event types (SPRINT_6 P-C):
          * ``model_decision``  — assistant text + (optional) tool_calls
          * ``tool_call_start`` — dispatch about to run, with id/name/args/turn
          * ``tool_call_end``   — dispatch finished, status=ok|error, content
          * ``loop_guard``      — guard tripped; no further dispatch
          * ``final``           — terminal: turn succeeded
          * ``max_turns``       — terminal: hit ``max_iterations``
          * ``error``           — runner-level exception surfaced

        Terminal events are always yielded last. A client disconnect raises
        ``GeneratorExit`` inside the queue.put(); we then cancel the task in
        ``finally:`` so no resources leak.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def _sink(ev: dict[str, Any]) -> None:
            await queue.put(ev)

        async def _runner() -> None:
            try:
                await self._run_loop(user_msg, session_id=session_id,
                                     channel_hint=channel_hint, sink=_sink)
            except Exception as exc:  # noqa: BLE001 — runner must surface failures as events
                # Class name only — match /chat/stream's contract. Full message
                # can carry internal paths / credentials / stack frames.
                await queue.put({"type": "error", "class": type(exc).__name__})
            finally:
                # Sentinel so the consumer breaks out even if no terminal event fired
                await queue.put({"type": "__end__"})

        task = asyncio.create_task(_runner())
        try:
            while True:
                ev = await queue.get()
                if ev.get("type") == "__end__":
                    break
                yield ev
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _run_loop(
        self,
        user_msg: str,
        *,
        session_id: str,
        channel_hint: str,
        sink: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> AgentResult:
        """Internal turn-loop. Same control flow as the original ask() — the
        only new behaviour is feeding events to ``sink`` at five hook points.

        When ``sink`` is None, behaves exactly like the pre-P-C ask() and
        returns an AgentResult. When ``sink`` is provided, the AgentResult is
        discarded; the caller (stream_ask) reconstructs terminal info from
        the events.
        """
        async def _emit(ev: dict[str, Any]) -> None:
            if sink is not None:
                await sink(ev)

        self._emit(EventType.AGENT_TURN_START, session=session_id)
        mem_block = self._memory.system_prompt_block() if self._memory else ""
        if self._store is not None:
            sys_prompt = restore_or_build_system_prompt(self._store, session_id, mem_block,
                                                        channel_hint=channel_hint)
            history = self._store.messages(session_id, limit=40)
        else:
            sys_prompt, history = system_prompt(mem_block, channel_hint=channel_hint), []
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
            await _emit({"type": "model_decision", "turn": turns,
                         "text": result.text,
                         "tool_calls": [{"id": c.id, "name": c.name,
                                         "arguments": c.arguments}
                                        for c in (result.tool_calls or [])]})
            if not result.tool_calls:
                final = result.text
                break
            messages.append(Message(role=Role.ASSISTANT, content=result.text,
                                    tool_calls=result.tool_calls))
            for call in result.tool_calls:
                args = _safe_args(call.arguments)
                reason = guard.check(call.name, args)
                if reason:
                    await _emit({"type": "loop_guard", "turn": turns,
                                 "tool": call.name, "reason": reason})
                    messages.append(Message(role=Role.TOOL,
                                            content=f"[loop-guard] {reason} — conclude or try a different approach",
                                            tool_call_id=call.id, name=call.name))
                    # One pivot turn without tools so the model can explain and conclude.
                    pivot = await self._router.complete(messages, system=sys_prompt, tools=None)
                    final = pivot.text or f"Stopped: {reason}"
                    await _emit({"type": "final", "turn": turns, "text": final,
                                 "tool_calls": len(tool_results)})
                    return self._finish(session_id, user_msg, final,
                                        tool_results, turns,
                                        outcome=TerminalOutcome.LOOP_GUARD)
                await _emit({"type": "tool_call_start", "turn": turns,
                             "id": call.id, "name": call.name,
                             "arguments": args})
                # Wrap dispatch so a single bad tool surfaces as a tool_call_end
                # error event instead of killing the stream mid-flight.
                try:
                    content, result_obj = await self._dispatch(call.name, args)
                except Exception as exc:  # noqa: BLE001
                    log.warning("orchestrator: tool %r dispatch raised: %s", call.name, exc)
                    content = f"[tool error: {type(exc).__name__}: {exc}]"
                    result_obj = None
                    await _emit({"type": "tool_call_end", "turn": turns,
                                 "id": call.id, "name": call.name,
                                 "status": "error", "content": content})
                    messages.append(Message(role=Role.TOOL, content=content,
                                            tool_call_id=call.id, name=call.name))
                    continue
                if result_obj is not None:
                    tool_results.append(result_obj)
                await _emit({"type": "tool_call_end", "turn": turns,
                             "id": call.id, "name": call.name,
                             "status": "ok", "content": content})
                messages.append(Message(role=Role.TOOL, content=content,
                                        tool_call_id=call.id, name=call.name))
        else:
            final = final or "[max turns reached]"
            # When stuck (no tool results) and a planner is wired in, suggest next steps.
            if self._planner is not None and not tool_results:
                try:
                    context = f"User: {user_msg[:500]}\nTurns used: {turns}"
                    plan = await self._planner.plan(self._goals or [user_msg], context)
                    if plan:
                        hint = "\n\nSuggested next steps:\n" + "\n".join(
                            f"- {t.get('task', '?')} (tool: {t.get('tool', '?')})"
                            for t in plan[:3]
                        )
                        final = final + hint
                except Exception as exc:  # noqa: BLE001 - planner hint is best-effort
                    log.warning("orchestrator: planner hint failed: %s", exc)
            await _emit({"type": "max_turns", "turn": turns, "text": final,
                         "tool_calls": len(tool_results)})
            return self._finish(session_id, user_msg, final, tool_results, turns,
                                outcome=TerminalOutcome.MAX_TURNS)

        await _emit({"type": "final", "turn": turns, "text": final,
                     "tool_calls": len(tool_results)})
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
                tool_results: list, turns: int,
                outcome: TerminalOutcome = TerminalOutcome.COMPLETED) -> AgentResult:
        if self._store is not None:
            self._store.append(session_id, Role.USER, user_msg)
            self._store.append(session_id, Role.ASSISTANT, final)
        if self._memory is not None:
            self._memory.sync_turn(user_msg, final, session_id=session_id)
        self._emit(EventType.AGENT_TURN_END, session=session_id, turns=turns)
        return AgentResult(content=final, tool_results=tool_results, turns=turns,
                           outcome=outcome)

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.publish(event_type, dict(data))
