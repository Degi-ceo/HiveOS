"""
compaction.py — head/tail-protected context compression (Hermes context_compressor).

Adapted from Hermes agent/context_compressor.py (docs/references/HERMES_REFERENCE.md
§"context_compressor"): when history grows past a trigger, protect the first `head`
turns (keeps the prefix-cache stable) and the last `tail` turns (recent continuity),
and replace the middle with an LLM summary using a structured template. The summary
is injected as a USER message so the cached system prefix never shifts.

Layer discipline: depends on hive.core ONLY. The model call is an injected
`Summarizer` (wired above to router.complete with TaskKind.AUX). Deterministic
fallback: if summarization fails or is empty, drop the middle and keep head+tail —
compaction never raises.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from hive.core.types import Message, Role

log = logging.getLogger("hive.context.compaction")

Summarizer = Callable[[list[Message], str], Awaitable[str]]

COMPACT_SYS = (
    "Summarize the earlier conversation for continuity. Be concise and structured "
    "under these headings: Active Task / In Progress / Pending / Resolved. Keep "
    "concrete identifiers (files, decisions, values). Output only the summary."
)


async def compact(
    messages: list[Message],
    *,
    summarizer: Summarizer,
    head: int = 2,
    tail: int = 6,
    trigger: int = 24,
) -> list[Message]:
    """Return a compacted copy of `messages` (or the original if under `trigger`)."""
    if len(messages) <= trigger or len(messages) <= head + tail:
        return list(messages)

    head_msgs = messages[:head]
    tail_msgs = messages[-tail:]
    middle = messages[head:-tail]
    if not middle:
        return list(messages)

    try:
        summary = (await summarizer(middle, COMPACT_SYS)).strip()
        if not summary:
            raise ValueError("empty summary")
    except Exception as exc:  # noqa: BLE001 - compaction must never raise
        log.warning("compaction fell back to truncation: %s", exc)
        return [*head_msgs, *tail_msgs]

    marker = Message(role=Role.USER, content=f"[Earlier conversation summary]\n{summary}")
    return [*head_msgs, marker, *tail_msgs]
