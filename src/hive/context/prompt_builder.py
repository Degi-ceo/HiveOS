"""
prompt_builder.py — deterministic prompt assembly for prefix-cache reuse.

Adapted from Hermes prompt_builder/system_prompt/prompt_caching + OpenJarvis
prompt/builder (docs/references/HERMES_REFERENCE.md §"prompt_builder"). Two rules
make Anthropic/MiniMax prefix caching effective:

  1. The persisted prefix is STABLE (SOUL + channel). Trusted top-memory is appended
     dynamically so corrections become visible without invalidating the cached prefix.
  2. Query-specific recalled memory is injected as a USER message, never into the
     stable prefix (Hermes AGENTS.md prompt-caching rule).

Depends on hive.core ONLY (SOUL + types).
"""
from __future__ import annotations

from typing import Protocol

from hive.core.soul import SOUL
from hive.core.types import Message, Role

_CACHE_VERSION_MARKER = "[HiveOS stable prompt cache v2]"


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
    """Restore the stable prefix and append current trusted memory each turn.

    Legacy cached prompts included a memory snapshot and are rebuilt once.  The
    persisted SOUL/channel prefix remains byte-exact while memory can be
    corrected or superseded without being frozen for the session."""
    existing = store.get_system_prompt(session_id)
    if existing is None or not existing.endswith(_CACHE_VERSION_MARKER):
        existing = system_prompt(channel_hint=channel_hint) + "\n\n" + _CACHE_VERSION_MARKER
        store.save_system_prompt(session_id, existing)
    return "\n\n".join(part for part in (existing, memory_block) if part)


def build_messages(
    history: list[Message], user_msg: str, *, recall_block: str = ""
) -> list[Message]:
    """History + (optional recall as a user message) + the new user turn."""
    msgs = list(history)
    if recall_block:
        msgs.append(Message(role=Role.USER, content=f"[Context from memory]\n{recall_block}"))
    msgs.append(Message(role=Role.USER, content=user_msg))
    return msgs
