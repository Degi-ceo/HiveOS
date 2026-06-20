"""
prompt_builder.py — deterministic prompt assembly for prefix-cache reuse.

Adapted from Hermes prompt_builder/system_prompt/prompt_caching + OpenJarvis
prompt/builder (docs/references/HERMES_REFERENCE.md §"prompt_builder"). Two rules
make Anthropic/MiniMax prefix caching effective:

  1. The system prompt is a STABLE prefix (SOUL + static memory guidance). It is
     persisted per session and restored BYTE-EXACT on later turns, even if inputs
     change — so the cached prefix matches (Hermes system_prompt + SessionDB).
  2. Per-turn dynamic context (recalled memory) is injected as a USER message, never
     into the system prompt, so the cached prefix never shifts (Hermes AGENTS.md
     prompt-caching rule).

Depends on hive.core ONLY (SOUL + types).
"""
from __future__ import annotations

from typing import Protocol

from hive.core.soul import SOUL
from hive.core.types import Message, Role


class SystemPromptStore(Protocol):
    def get_system_prompt(self, session_id: str) -> str | None: ...
    def save_system_prompt(self, session_id: str, text: str) -> None: ...


def system_prompt(memory_block: str = "", channel_hint: str = "") -> str:
    """Assemble the stable system prefix (deterministic ordering)."""
    parts = [SOUL]
    if channel_hint:
        parts.append(f"[Active surface: {channel_hint}]")
    if memory_block:
        parts.append(memory_block)
    return "\n\n".join(parts)


def restore_or_build_system_prompt(
    store: SystemPromptStore, session_id: str, memory_block: str = "",
    channel_hint: str = "",
) -> str:
    """Byte-exact restore for prefix-cache reuse; build + persist on first turn.

    channel_hint is baked in on first build so later turns get a byte-exact
    cache hit instead of a miss caused by appending it after restore."""
    existing = store.get_system_prompt(session_id)
    if existing is not None:
        return existing
    text = system_prompt(memory_block, channel_hint=channel_hint)
    store.save_system_prompt(session_id, text)
    return text


def build_messages(
    history: list[Message], user_msg: str, *, recall_block: str = ""
) -> list[Message]:
    """History + (optional recall as a user message) + the new user turn."""
    msgs = list(history)
    if recall_block:
        msgs.append(Message(role=Role.USER, content=f"[Context from memory]\n{recall_block}"))
    msgs.append(Message(role=Role.USER, content=user_msg))
    return msgs
