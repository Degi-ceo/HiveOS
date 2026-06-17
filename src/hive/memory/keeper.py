"""
keeper.py — the memory-keeper (sleep-time consolidation).

Ported from Memory/memory_keeper.py. Runs off the cheap aux model to reflect over
recent turns and extract ONLY durable, reusable knowledge, which it persists via
the provider (which also promotes to the vault). Keeps the primary loop fast.

Layer discipline: this module depends on `hive.core` ONLY. The aux-model call is an
injected `Summarizer` (wired above, in the agents/builder layer, to the router with
TaskKind.AUX) so the memory layer never imports the llm layer.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Protocol

from hive.core.types import Message, Role

log = logging.getLogger("hive.memory.keeper")

# (messages, system_prompt) -> raw model text. Wired to router.complete(...).text.
Summarizer = Callable[[list[Message], str], Awaitable[str]]

REFLECT_SYS = (
    "You are Hive's memory-keeper. From the work log, extract ONLY durable, reusable "
    'knowledge as a JSON list of {"kind","topic","content","source"} where kind is one '
    "of skill|mcp|research|fix|fact. Skip ephemera. Return ONLY JSON."
)


class _Learnable(Protocol):
    def recent(self, session: str = ..., limit: int = ...) -> list[dict]: ...
    def already_known(self, topic: str) -> bool: ...
    def learn(self, kind: str, topic: str, content: str, source: str = ...) -> None: ...


def _parse_items(raw: str) -> list[dict]:
    cleaned = raw.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:]
    data = json.loads(cleaned)
    return data if isinstance(data, list) else []


class MemoryKeeper:
    def __init__(self, summarize: Summarizer, provider: _Learnable) -> None:
        self._summarize = summarize
        self._provider = provider
        self._last_consolidated_ts: float | None = None
        self._last_consolidated_count: int = 0

    @property
    def last_consolidated_ts(self) -> float | None:
        """Timestamp of the last successful consolidation, or None if never run."""
        return self._last_consolidated_ts

    @property
    def last_consolidated_count(self) -> int:
        """Number of items extracted in the last consolidation run."""
        return self._last_consolidated_count

    async def consolidate(self, session: str = "", limit: int = 40) -> int:
        """Reflect over recent turns; persist new durable learnings. Returns count."""
        turns = self._provider.recent(session, limit)
        if not turns:
            return 0
        log_text = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        try:
            raw = await self._summarize([Message(role=Role.USER, content=log_text)], REFLECT_SYS)
            items = _parse_items(raw)
        except Exception as exc:  # noqa: BLE001 - consolidation is best-effort
            log.warning("consolidate failed: %s", exc)
            return 0

        new = 0
        for item in items:
            # Per-item guard: a DB error on one item must not abort consolidation (best-effort).
            try:
                topic = str(item.get("topic", "")).strip()
                if not topic or self._provider.already_known(topic):
                    continue
                self._provider.learn(
                    str(item.get("kind", "fact")), topic,
                    str(item.get("content", "")), str(item.get("source", session)),
                )
                new += 1
            except Exception as exc:  # noqa: BLE001 - skip the bad item, keep going
                log.warning("consolidate could not persist %r: %s", item, exc)
        import time as _time
        self._last_consolidated_ts = _time.time()
        self._last_consolidated_count = new
        log.info("memory-keeper consolidated %d new items", new)
        return new
