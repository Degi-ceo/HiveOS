"""
keeper.py — the memory-keeper (sleep-time consolidation).

Ported from Memory/memory_keeper.py. Runs off the cheap aux model to reflect over
recent turns and extract ONLY durable, reusable knowledge, which it persists via
the provider (which also promotes to the vault). Keeps the primary loop fast.

Layer discipline: this module depends on `hive.core` ONLY. The aux-model call is an
injected `Summarizer` (wired above, in the agents/builder layer, to the router with
TaskKind.AUX) so the memory layer never imports the llm layer.

SPRINT_7 Batch D — entity resolution: when ``use_entity_resolution=True`` (default),
the keeper groups facts that share a canonical key (after EntityResolver normalization)
and skips re-learning a surface form whose canonical entity is already known.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Protocol

from hive.core.types import Message, Role
from hive.memory.entity_resolver import EntityResolver

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
    def __init__(self, summarize: Summarizer, provider: _Learnable, *,
                 resolver: EntityResolver | None = None) -> None:
        self._summarize = summarize
        self._provider = provider
        # Default resolver: pure normalisation, no alias map. The alias map can be
        # supplied by the builder/runtime when config.entity_resolution_alias_map
        # is non-empty. Keeping a default-constructed resolver keeps the fast-path
        # cheap (no alias map lookups).
        self._resolver = resolver or EntityResolver()
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

    @property
    def resolver(self) -> EntityResolver:
        """The EntityResolver instance (exposed for tests and diagnostics)."""
        return self._resolver

    async def consolidate(self, session: str = "", limit: int = 40, *,
                          use_entity_resolution: bool = True) -> int:
        """Reflect over recent turns; persist new durable learnings. Returns count.

        When ``use_entity_resolution`` is True (default) facts that share a
        canonical key (e.g. "PR #95" / "pr_95") are merged and ``already_known``
        is checked against the canonical key, not the literal surface. The set
        of seen canonical keys is kept in-memory for the duration of the call
        so duplicate aliases don't double-count.

        Backwards-compat: pass ``use_entity_resolution=False`` to get the
        pre-SPRINT_7 behaviour (per-surface ``already_known`` check, no merging).
        """
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
        # Tracks canonical keys we've already learned (or skipped) within this
        # run so duplicates don't double-count. Cleared automatically per call.
        seen_canonical: set[str] = set()
        for item in items:
            # Per-item guard: a DB error on one item must not abort consolidation (best-effort).
            try:
                topic = str(item.get("topic", "")).strip()
                if not topic:
                    continue
                if use_entity_resolution:
                    key = self._resolver.canonical_key(topic)
                    if key in seen_canonical:
                        continue
                    # The resolver builds canonical -> observed-alias tracking;
                    # treat it as the canonical surface for the already_known check.
                    learned = self._learn_with_resolution(
                        item, topic, key, session, seen_canonical,
                    )
                else:
                    learned = self._learn_legacy(item, topic, session)
                if learned:
                    new += 1
            except Exception as exc:  # noqa: BLE001 - skip the bad item, keep going
                log.warning("consolidate could not persist %r: %s", item, exc)
        import time as _time
        self._last_consolidated_ts = _time.time()
        self._last_consolidated_count = new
        log.info("memory-keeper consolidated %d new items (entity_resolution=%s)",
                 new, use_entity_resolution)
        return new

    def _learn_with_resolution(self, item: dict, topic: str, key: str,
                               session: str, seen_canonical: set[str]) -> bool:
        """Learn a fact under entity resolution. Returns True if persisted."""
        # 1. Skip if the canonical entity is already known.
        if self._provider.already_known(key):
            seen_canonical.add(key)
            return False
        # 2. Persist under the canonical key so future calls collapse.
        source = str(item.get("source", session))
        try:
            self._provider.learn(
                str(item.get("kind", "fact")), key,
                str(item.get("content", "")), source,
            )
        except Exception:
            # Fall back to the original surface so we don't lose facts.
            log.debug("learn(canonical=%s) failed, retrying with surface %s", key, topic)
            self._provider.learn(
                str(item.get("kind", "fact")), topic,
                str(item.get("content", "")), source,
            )
        seen_canonical.add(key)
        # 3. Side-record the original surface form in aliases for traceability.
        #    Storing via learn() above (with kind='alias' kind) keeps the audit
        #    trail; failure is logged and ignored.
        if key != topic:
            try:
                self._provider.learn(
                    "alias", f"{key}::{topic}",
                    f"surface form for {key}",
                    source,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("alias side-record failed (non-fatal): %s", exc)
        return True

    def _learn_legacy(self, item: dict, topic: str, session: str) -> bool:
        """Pre-SPRINT_7 behaviour: per-surface already_known check, no merge."""
        if self._provider.already_known(topic):
            return False
        self._provider.learn(
            str(item.get("kind", "fact")), topic,
            str(item.get("content", "")), str(item.get("source", session)),
        )
        return True
