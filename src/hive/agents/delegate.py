"""
delegate.py — isolated parallel subagents (ADAPT of Hermes delegate_tool.py).

Spawns leaf child agents to run subtasks concurrently with a bounded semaphore.
Subagents are LEAVES: the injected `agent_factory` must build agents WITHOUT a
delegation capability, so nesting cannot recurse (the safe-nesting rule from the
old orchestrator). Each child gets a fresh agent + fresh context.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from hive.agents.base import AgentResult, BaseAgent
from hive.agents.executor import AgentExecutor, TerminalOutcome
from hive.core.events import EventBus  # noqa: F401 - re-exported for typing

AgentFactory = Callable[[], BaseAgent]

# Named registry: maps agent name → factory. Populated by HiveOS.build() or tests.
# Allows `delegate_named(task, "researcher")` instead of passing a factory function.
_AGENT_REGISTRY: dict[str, AgentFactory] = {}


def register_agent(name: str, factory: AgentFactory) -> None:
    """Register a named agent factory (called at build time or in tests)."""
    _AGENT_REGISTRY[name] = factory


def get_agent_factory(name: str) -> AgentFactory:
    """Retrieve a named agent factory; raises KeyError if not registered."""
    if name not in _AGENT_REGISTRY:
        raise KeyError(f"no agent registered under {name!r}; available: {sorted(_AGENT_REGISTRY)}")
    return _AGENT_REGISTRY[name]


async def delegate_named(
    subtasks: list[str], name: str, *, max_concurrent: int = 3,
    executor: AgentExecutor | None = None,
) -> list[AgentResult]:
    """Like `delegate()` but resolves the agent factory by name from the registry."""
    return await delegate(subtasks, agent_factory=get_agent_factory(name),
                          max_concurrent=max_concurrent, executor=executor)


async def delegate_via_envelope(
    task: str, name: str, *, executor: AgentExecutor | None = None,
    bus: EventBus | None = None,
    session_id: str | None = None,
) -> AgentResult:
    """Route a single subtask through the A2A envelope (SPRINT_6 P-D, issue #72).

    On the v1 path used by ``delegate_to_specialist``, no bus is passed and no
    events are emitted (backward-compat). When a bus is supplied (P-G Kanban
    wiring), publishes a2a.call.{started,completed,failed} around the handler.

    Registers a local handler on first call for the named agent, then routes an
    A2ARequest through ``hive.agents.a2a.router.route``. Returns an
    ``AgentResult`` with the envelope's result or an error message.
    """
    from hive.agents.a2a.envelope import A2ARequest
    from hive.agents.a2a.events import (
        emit_call_completed,
        emit_call_failed,
        emit_call_started,
    )
    from hive.agents.a2a.router import register as _a2a_register
    from hive.agents.a2a.router import route as _a2a_route

    method = f"{name}.run"

    async def _handler(params: dict[str, object]) -> str:
        factory = get_agent_factory(name)
        ex = executor or AgentExecutor()
        tick = await ex.execute_tick(factory(), str(params.get("task", "")))
        if tick.outcome is TerminalOutcome.COMPLETED and tick.result is not None:
            return tick.result.content
        return f"[subagent failed: {tick.error}]"

    _a2a_register(method, _handler)
    req = A2ARequest(method=method, params={"task": task})
    if bus is not None:
        emit_call_started(
            bus, method=method, request_id=req.id, agent_name=name,
            task=task, session_id=session_id,
        )
    try:
        resp = await _a2a_route(req.id, method, req.params)
    except Exception as exc:  # noqa: BLE001 - normalise + emit failed
        if bus is not None:
            emit_call_failed(
                bus, method=method, request_id=req.id, agent_name=name,
                error=f"{type(exc).__name__}: {exc}",
            )
        return AgentResult(content=f"[delegate error: {exc}]")
    if resp.is_error():
        if bus is not None:
            emit_call_failed(
                bus, method=method, request_id=req.id, agent_name=name,
                error=resp.error.message,
            )
        return AgentResult(content=f"[delegate error: {resp.error.message}]")
    content = resp.result if isinstance(resp.result, str) else str(resp.result)
    if bus is not None:
        if content.startswith("[subagent failed:"):
            emit_call_failed(
                bus, method=method, request_id=req.id, agent_name=name,
                error=content,
            )
        else:
            emit_call_completed(
                bus, method=method, request_id=req.id, agent_name=name,
                result=resp.result,
            )
    return AgentResult(content=content)


async def delegate(
    subtasks: list[str], *, agent_factory: AgentFactory, max_concurrent: int = 3,
    executor: AgentExecutor | None = None,
) -> list[AgentResult]:
    """Run each subtask on a fresh leaf agent, ≤max_concurrent at a time (order preserved).

    Each subagent runs through an AgentExecutor (retry on transient failure + normalized
    terminal outcome), so a flaky subagent retries instead of poisoning the batch; a
    permanently-failing one yields an error AgentResult rather than raising (A5)."""
    sem = asyncio.Semaphore(max_concurrent)
    ex = executor or AgentExecutor()

    async def run_one(task: str) -> AgentResult:
        async with sem:
            tick = await ex.execute_tick(agent_factory(), task)
            if tick.outcome is TerminalOutcome.COMPLETED and tick.result is not None:
                return tick.result
            return AgentResult(content=f"[subagent failed: {tick.error}]")

    return await asyncio.gather(*(run_one(t) for t in subtasks))
