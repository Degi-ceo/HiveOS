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

from hive.core.events import EventType
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
        self._tick_count = 0
        self._last_proactive_ts: float = float("-inf")  # ensures first run always fires

    def enqueue(self, task: dict) -> int:
        """Durably enqueue a task (survives restart). Returns the task id."""
        return self._hive.task_board.enqueue("tool", task, source="manual")

    async def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._hive.events.publish(EventType.AGENT_TICK_START, {"ts": now})
        result = await self._tick_inner(now)
        self._hive.events.publish(EventType.AGENT_TICK_END, {"ts": time.time(),
                                                               **result})
        return result

    async def _tick_inner(self, now: float) -> dict:
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
        try:
            consolidated = await self._hive.consolidate()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: consolidation failed: %s", exc)
            consolidated = 0
        curation = self._hive.curate()  # deterministic skill lifecycle (safe, no-op early)
        try:
            await self._hive.curate_umbrellas()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: curator umbrellas failed: %s", exc)
        try:
            await self._refresh_budget()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: budget refresh failed: %s", exc)
        curated = len(curation.get("transitions", []))
        # 4. After dispatch: check for repeated failures and trigger self-improvement.
        #    Only fire when ≥3 recent failures to avoid over-reacting to transients.
        self_improved = 0
        try:
            threshold = self._hive.config.selfmod_failure_threshold
            failed = self._hive.task_board.recent_failures(limit=10)
            if len(failed) >= threshold:
                symptom = ("Repeated task failures in last tick: "
                           + "; ".join(t.last_error or "unknown" for t in failed[:5]))
                outcomes = await self._hive.self_improve_from_symptom(symptom)
                self_improved = len(outcomes)
        except Exception as exc:  # noqa: BLE001 - self-improve failure must not abort tick
            log.warning("heartbeat: self-improve check failed: %s", exc)

        # 5. Proactive self-diagnose: run every N ticks, but throttle idle runs.
        self._tick_count += 1
        proactive_diagnosed = 0
        interval = getattr(self._hive.config, "selfmod_proactive_interval", 10)
        _PROACTIVE_COOLDOWN = 1800  # 30 min between zero-outcome runs
        if interval > 0 and self._tick_count % interval == 0:
            elapsed = now - self._last_proactive_ts
            if elapsed >= _PROACTIVE_COOLDOWN:
                try:
                    log.info("heartbeat: proactive self-diagnose (tick %d)", self._tick_count)
                    result = await self._hive.self_diagnose()
                    proactive_diagnosed = len(result.get("improvement_outcomes", []))
                    log.info("heartbeat: proactive self-diagnose: %d outcome(s)", proactive_diagnosed)
                    self._last_proactive_ts = now
                except Exception as exc:  # noqa: BLE001 - proactive diagnose must not abort tick
                    log.warning("heartbeat: proactive self-diagnose failed: %s", exc)
            else:
                log.info("heartbeat: proactive self-diagnose skipped (cooldown %.0fs remaining)",
                         _PROACTIVE_COOLDOWN - elapsed)

        log.info("heartbeat: cron=%d commitments=%d planned=%d dispatched=%d "
                 "consolidated=%d curated=%d self_improved=%d proactive_diagnosed=%d",
                 cron_fired, commitments_fired, planned, dispatched, consolidated,
                 curated, self_improved, proactive_diagnosed)
        return {"cron": cron_fired, "commitments": commitments_fired, "planned": planned,
                "dispatched": dispatched, "consolidated": consolidated, "curated": curated,
                "self_improved": self_improved, "proactive_diagnosed": proactive_diagnosed}

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
        # On startup, recover any tasks that were RUNNING when the process was killed.
        recovered = self._hive.task_board.requeue_running()
        if recovered:
            log.info("heartbeat: recovered %d RUNNING task(s) left from prior run", recovered)
        log.info("heartbeat loop started (interval=%ss)", period)
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
                log.error("heartbeat tick error: %s", exc, exc_info=True)
            await asyncio.sleep(period)

    def stop(self) -> None:
        self._running = False
