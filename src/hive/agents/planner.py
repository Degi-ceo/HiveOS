"""
planner.py — goals + state -> task list (KEEP from Core/planner.py).

Heavy/novel planning routes to the ChatGPT Plus planner (TaskKind.PLAN) when
enabled; otherwise the MiniMax executor plans. Output is a JSON task list the
orchestrator dispatches. The router is injected (agents depend on llm).
"""
from __future__ import annotations

import json
import logging

from hive.core.soul import SOUL
from hive.core.types import Message, Role
from hive.llm.router import ModelRouter, TaskKind

log = logging.getLogger("hive.agents.planner")

PLANNER_SYS = (
    SOUL + "\n\n"
    "You are Hive's planner. Given goals and recent context, output the next 1-3 "
    'concrete tasks as a JSON list of {"task","tool","args","reason"}. Apply '
    "discovery-first: if a task needs a new capability, the first task must be a "
    "discovery/audit step. Output ONLY JSON."
)


def _parse(raw: str) -> list[dict]:
    cleaned = raw.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:]
    data = json.loads(cleaned)
    return data if isinstance(data, list) else [data]


class Planner:
    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    async def plan(self, goals: list[str], context: str, *, heavy: bool = False) -> list[dict]:
        kind = TaskKind.PLAN if heavy else TaskKind.EXECUTE
        prompt = f"GOALS:\n{json.dumps(goals)}\n\nCONTEXT:\n{context}"
        result = await self._router.complete(
            [Message(role=Role.USER, content=prompt)],
            kind=kind, system=PLANNER_SYS, max_tokens=2048,
        )
        try:
            return _parse(result.text)
        except Exception as exc:  # noqa: BLE001 - bad JSON yields no tasks, never crashes
            log.warning("plan parse failed: %s | raw=%s", exc, result.text[:200])
            return []
