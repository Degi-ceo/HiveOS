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

AgentFactory = Callable[[], BaseAgent]


async def delegate(
    subtasks: list[str], *, agent_factory: AgentFactory, max_concurrent: int = 3,
) -> list[AgentResult]:
    """Run each subtask on a fresh leaf agent, ≤max_concurrent at a time (order preserved)."""
    sem = asyncio.Semaphore(max_concurrent)

    async def run_one(task: str) -> AgentResult:
        async with sem:
            return await agent_factory().run(task)

    return await asyncio.gather(*(run_one(t) for t in subtasks))
