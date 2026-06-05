"""
memory_keeper.py — the memory-keeper sub-agent (sleep-time compute).

Runs on the cheap AUX model. It is the ONLY component that consolidates and
promotes long-term memory, so the primary Hive loop stays fast. On a schedule:
  1. Reflect over recent working memory (generative-agents style) and extract
     durable facts/skills/research outcomes.
  2. Persist them via brain.learn(), which also promotes to the Obsidian vault.
  3. (If Mnemosyne MCP is wired) trigger mnemosyne sleep/evolve consolidation.

This realizes Kamil's rule: once learned, always remembered and reusable.
"""
from __future__ import annotations
import json
import logging

from core import settings
from core.model_router import router, TaskKind
from memory.brain import brain

log = logging.getLogger("hiveos.keeper")

REFLECT_SYS = (
    "You are Hive's memory-keeper. From the conversation/work log, extract ONLY "
    "durable, reusable knowledge as a JSON list of objects "
    '{"kind","topic","content","source"} where kind is one of '
    "skill|mcp|research|fix|fact. Skip ephemera. Return ONLY JSON."
)


async def consolidate(bank: str = "default") -> int:
    turns = brain.recent(bank, 40)
    if not turns:
        return 0
    log_text = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
    try:
        raw = await router.complete(
            [{"role": "user", "content": log_text}],
            kind=TaskKind.AUX, system=REFLECT_SYS, thinking=False, max_tokens=2048,
        )
        cleaned = raw.strip().strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        items = json.loads(cleaned)
    except Exception as e:  # noqa: BLE001
        log.warning("consolidate parse failed: %s", e)
        return 0
    n = 0
    for it in items:
        topic = it.get("topic", "").strip()
        if not topic or brain.already_known(topic):
            continue
        brain.learn(it.get("kind", "fact"), topic,
                    it.get("content", ""), it.get("source", bank))
        n += 1
    log.info("memory-keeper consolidated %d new items", n)
    return n
