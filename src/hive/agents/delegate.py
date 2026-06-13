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
