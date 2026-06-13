"""
heartbeat.py — the never-idle autonomy loop (KEEP from Core/orchestrator.py).

Each tick (M3): fire due cron jobs + commitments onto the durable TaskBoard; if the
board has no due work, plan the next 1-3 tasks from goals + memory and enqueue them
too; then claim and dispatch the due tasks through the gate-routed ToolExecutor
(bounded concurrency), marking each done/failed on the board; finally run sleep-time
memory consolidation (keeper) + skill-lifecycle curation and refresh the budget.

The board is SQLite-backed, so queued work survives a restart and is drained on the
next tick. Subagents are leaves — dispatch executes tools, it does not spawn nested
heartbeats. Drives an assembled HiveOS; `tick()` is one cycle, `run()` is the 24/7 loop.
"""
from __future__ import annotations

import asyncio
import logging
import time

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
        self._sem = asyncio.Semaphore(max(1, hive.config.max_concurrent_agents))
        self._running = False

    def enqueue(self, task: dict) -> int:
        """Durably enqueue a task (survives restart). Returns the task id."""
        return self._hive.task_board.enqueue("tool", task, source="manual")

    async def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        # 1. Schedulers populate the durable board.
        cron_fired = self._hive.cron.due_and_enqueue(now)
        commitments_fired = self._hive.commitments.due_and_enqueue(now)

        # 2. If nothing is due, plan fresh work and enqueue it onto the board.
        due = self._hive.task_board.due(now)
        planned = 0
        if not due:
            context = self._hive.memory.prefetch("recent tasks goals progress") or "fresh start"
            plan = await self._hive.planner.plan(self._goals, context)
            for task in plan:
                self._hive.task_board.enqueue("tool", task, source="planner")
            planned = len(plan)
            due = self._hive.task_board.due(now)

        # 3. Claim + dispatch the due tasks; record outcome on the board.
        dispatched = await self._dispatch(due)
        consolidated = await self._hive.consolidate()
        curation = self._hive.curate()  # deterministic skill lifecycle (safe, no-op early)
        await self._refresh_budget()
        curated = len(curation.get("transitions", []))
        # 4. After dispatch: check for repeated failures and trigger self-improvement.
        #    Only fire when ≥3 recent failures to avoid over-reacting to transients.
        self_improved = 0
        try:
            failed = self._hive.task_board.recent_failures(limit=10)
            if len(failed) >= 3:
                symptom = ("Repeated task failures in last tick: "
                           + "; ".join(t.last_error or "unknown" for t in failed[:5]))
                outcomes = await self._hive.self_improve_from_symptom(symptom)
                self_improved = len(outcomes)
        except Exception as exc:  # noqa: BLE001 - self-improve failure must not abort tick
            log.warning("heartbeat: self-improve check failed: %s", exc)

        log.info("heartbeat: cron=%d commitments=%d planned=%d dispatched=%d "
                 "consolidated=%d curated=%d self_improved=%d",
                 cron_fired, commitments_fired, planned, dispatched, consolidated,
                 curated, self_improved)
        return {"cron": cron_fired, "commitments": commitments_fired, "planned": planned,
                "dispatched": dispatched, "consolidated": consolidated, "curated": curated,
                "self_improved": self_improved}

    async def _dispatch(self, tasks: list) -> int:
        board = self._hive.task_board

        async def run_one(record) -> bool:
            if not board.claim(record.id):
                return False  # already claimed by a concurrent drain
            payload = record.payload
            tool = payload.get("tool")
            if not tool:
                board.complete(record.id)  # nothing executable; consider it handled
                return False
            async with self._sem:
                try:
                    await self._hive.tool_executor.execute(
                        tool, payload.get("args", {}), reason=payload.get("reason", ""))
                    board.complete(record.id)
                    return True
                except Exception as exc:  # noqa: BLE001 - one bad task must not abort the tick
                    board.fail(record.id, str(exc))
                    log.warning("task %s failed: %s", record.id, exc)
                    return False

        results = await asyncio.gather(*(run_one(t) for t in tasks))
        return sum(1 for ok in results if ok)

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
                log.error("heartbeat tick error: %s", exc, exc_info=True)
            await asyncio.sleep(period)

    def stop(self) -> None:
        self._running = False
