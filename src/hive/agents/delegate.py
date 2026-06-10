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
