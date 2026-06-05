"""
orchestrator.py — Hive's autonomy engine (the 24/7 loop).

Each heartbeat:
  1. If user tasks are queued -> plan + dispatch them.
  2. Else (NEVER idle) -> run a gap-analysis curriculum step: scan recent
     failures + capabilities, find something to improve, queue it.
  3. Dispatch tasks to subagents (orchestrator-worker, max 3 concurrent).
     Dangerous tasks surface as approvals instead of executing.
  4. Run the memory-keeper consolidation (sleep-time compute).

Subagents are leaves: they cannot spawn further subagents (safe nesting).

Run:  python -m core.orchestrator
"""
from __future__ import annotations
import json
import asyncio
import logging
from pathlib import Path

from core import settings
from core.planner import plan
from tools import registry
from tools.discovery import discover
from memory.brain import brain
from memory.memory_keeper import consolidate

log = logging.getLogger("hiveos.orchestrator")


def _load_goals() -> list[str]:
    p = Path(settings.ROOT) / "config" / "goals.json"
    if p.exists():
        return json.loads(p.read_text()).get("goals", [])
    return ["Keep projects moving and surface blockers.",
            "Continuously find gaps and improve HiveOS."]


GAP_SYS = (
    "You are Hive's improvement scout. From the capability list and recent "
    "failures, name ONE concrete, safe improvement to pursue now as JSON "
    '{"task","tool","args","reason"}. Prefer discovery-first. Only JSON.'
)


class Orchestrator:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.MAX_CONCURRENT_AGENTS)
        self._running = False
        self._queue: list[dict] = []

    def enqueue(self, task: dict) -> None:
        self._queue.append(task)

    async def _subagent(self, task: dict) -> None:
        """A leaf worker: executes one task via a tool. Cannot spawn children."""
        async with self._sem:
            tool = task.get("tool")
            if not tool:
                return
            # discovery-first: if the task is a discovery step, run it specially
            if tool == "discover":
                res = await discover(task.get("args", {}).get("need", ""),
                                     settings.GITHUB_TOKEN)
            else:
                res = await registry.call(tool, task.get("args", {}),
                                          task.get("reason", ""))
            brain.remember(f"task={task.get('task')} -> {res}", role="task")
            log.info("task=%s -> %s", task.get("task"),
                     res.get("status", "ok") if isinstance(res, dict) else "ok")

    async def _gap_step(self) -> list[dict]:
        """Never idle: ask for one improvement to pursue."""
        from core.model_router import router, TaskKind
        caps = registry.list_tools()
        ctx = f"capabilities={caps}\nrecent={[t['content'][:120] for t in brain.recent(limit=10)]}"
        try:
            raw = await router.complete(
                [{"role": "user", "content": ctx}],
                kind=TaskKind.AUX, system=GAP_SYS, thinking=False, max_tokens=512)
            c = raw.strip().strip("`")
            if c.startswith("json"):
                c = c[4:]
            t = json.loads(c)
            return t if isinstance(t, list) else [t]
        except Exception as e:  # noqa: BLE001
            log.debug("gap step skipped: %s", e)
            return []

    async def heartbeat(self) -> None:
        log.info("♥ heartbeat (queue=%d)", len(self._queue))
        if self._queue:
            tasks, self._queue = self._queue, []
        else:
            goals = _load_goals()
            ctx = "\n".join(t["content"] for t in brain.recent(limit=15)) or "fresh start"
            tasks = await plan(goals, ctx)
            if not tasks:
                tasks = await self._gap_step()
        if tasks:
            await asyncio.gather(*(self._subagent(t) for t in tasks))
        await consolidate("default")

    async def run(self) -> None:
        self._running = True
        log.info("Orchestrator started (heartbeat=%ds, max_agents=%d)",
                 settings.HEARTBEAT_SEC, settings.MAX_CONCURRENT_AGENTS)
        while self._running:
            try:
                await self.heartbeat()
            except Exception as e:  # noqa: BLE001
                log.error("heartbeat error: %s", e)
            await asyncio.sleep(settings.HEARTBEAT_SEC)

    def stop(self) -> None:
        self._running = False


orchestrator = Orchestrator()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(orchestrator.run())
