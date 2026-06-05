"""
planner.py — turns goals + state into a task list.

Heavy/novel planning routes to the ChatGPT Plus planner (TaskKind.PLAN) when
enabled; otherwise the MiniMax executor plans. Output is a JSON task list the
orchestrator dispatches to subagents.
"""
from __future__ import annotations
import json
import logging

from core import settings
from core.model_router import router, TaskKind

log = logging.getLogger("hiveos.planner")

PLANNER_SYS = (
    settings.SOUL + "\n\n"
    "You are Hive's planner. Given goals and recent context, output the next "
    "1-3 concrete tasks as a JSON list of objects "
    '{"task","tool","args","reason"}. Apply discovery-first: if a task needs a '
    "new capability, the first task must be a discovery/audit step. Only output JSON."
)


async def plan(goals: list[str], context: str, heavy: bool = False) -> list[dict]:
    kind = TaskKind.PLAN if heavy else TaskKind.EXECUTE
    raw = await router.complete(
        [{"role": "user", "content": f"GOALS:\n{json.dumps(goals)}\n\nCONTEXT:\n{context}"}],
        kind=kind, system=PLANNER_SYS, thinking=True, max_tokens=2048,
    )
    try:
        c = raw.strip().strip("`")
        if c.startswith("json"):
            c = c[4:]
        return json.loads(c)
    except Exception as e:  # noqa: BLE001
        log.warning("plan parse failed: %s | raw=%s", e, raw[:200])
        return []
