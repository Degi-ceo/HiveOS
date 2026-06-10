"""
title.py — session auto-naming (M7 B3, Hermes title_generator #27).

Generates a short human title from the first user message via an injected
summarizer (the same aux-model callable the orchestrator already holds), so context
stays DAG-clean (no llm import here). Best-effort: failures yield "Untitled".
"""
from __future__ import annotations

from typing import Awaitable, Callable

from hive.core.types import Message, Role

# Same shape as the orchestrator's summarizer: (messages, system) -> text.
Summarizer = Callable[[list[Message], str], Awaitable[str]]

_SYSTEM = ("Generate a concise 3-6 word title for a conversation that starts with the "
           "user's message. Reply with ONLY the title — no quotes, no punctuation at the end.")


async def generate_title(first_message: str, summarizer: Summarizer) -> str:
    msgs = [Message(role=Role.USER, content=first_message[:500])]
    try:
        raw = await summarizer(msgs, _SYSTEM)
    except Exception:  # noqa: BLE001 - titling is best-effort
        return "Untitled"
    title = (raw or "").strip().strip('"').strip("'").splitlines()[0:1]
    cleaned = title[0].strip() if title else ""
    return cleaned[:80] or "Untitled"
