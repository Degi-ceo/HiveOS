"""
heartbeat.py — the never-idle autonomy loop (KEEP from Core/orchestrator.py).

Each tick: drain the task queue, else plan the next 1-3 tasks from goals + recent
memory (planner) and dispatch them through the gate-routed ToolExecutor (bounded
concurrency); then run sleep-time memory consolidation (keeper) and refresh the
budget window. Subagents are leaves — dispatch executes tools, it does not spawn
nested heartbeats. Drives an assembled HiveOS; `tick()` is one cycle (the P8
verify), `run()` is the 24/7 loop.
"""
from __future__ import annotations

import asyncio
import logging

from hive.runtime import HiveOS

log = logging.getLogger("hive.autonomy.heartbeat")

_DEFAULT_GOALS = (
    "Keep projects moving and surface blockers.",
    "Continuously find gaps and improve HiveOS.",
)


class Heartbeat:
    def __init__(self, hive: HiveOS, *, goals: list[str] | None = None) -> None:
        self._hive = hive
        self._goals = list(goals or _DEFAULT_GOALS)
        self._queue: list[dict] = []
        self._sem = asyncio.Semaphore(max(1, hive.config.max_concurrent_agents))
        self._running = False

    def enqueue(self, task: dict) -> None:
        self._queue.append(task)

    async def tick(self) -> dict:
        if self._queue:
            tasks, self._queue = self._queue, []
        else:
            # prefetch() is in the MemoryProvider ABC and works with all providers.
            context = self._hive.memory.prefetch("recent tasks goals progress") or "fresh start"
            tasks = await self._hive.planner.plan(self._goals, context)

        dispatched = await self._dispatch(tasks)
        consolidated = await self._hive.consolidate()
        await self._refresh_budget()
        log.info("heartbeat: planned=%d dispatched=%d consolidated=%d",
                 len(tasks), dispatched, consolidated)
        return {"planned": len(tasks), "dispatched": dispatched, "consolidated": consolidated}

    async def _dispatch(self, tasks: list[dict]) -> int:
        async def run_one(task: dict):
            tool = task.get("tool")
            if not tool:
                return None
            async with self._sem:
                return await self._hive.tool_executor.execute(
                    tool, task.get("args", {}), reason=task.get("reason", ""))

        # return_exceptions: one bad task must not abort the cycle (skip consolidate/budget)
        results = await asyncio.gather(*(run_one(t) for t in tasks), return_exceptions=True)
        dispatched = 0
        for r in results:
            if isinstance(r, Exception):
                log.warning("dispatched task failed: %s", r)
            elif r is not None:
                dispatched += 1
        return dispatched

    async def _refresh_budget(self) -> None:
        cfg = self._hive.config
        await self._hive.budgeter.refresh(cfg.minimax_api_key, cfg.remains_url)

    async def run(self, *, interval: float | None = None) -> None:
        self._running = True
        period = interval if interval is not None else self._hive.config.heartbeat_sec
        log.info("heartbeat loop started (interval=%ss)", period)
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
                log.error("heartbeat tick error: %s", exc)
            await asyncio.sleep(period)

    def stop(self) -> None:
        self._running = False
