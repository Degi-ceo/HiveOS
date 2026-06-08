"""
provider.py — MemoryProvider ABC (single active memory slot).

Contract combines Hermes's MemoryProvider lifecycle (HERMES_REFERENCE §6) with
OpenClaw's single-active-memory-slot rule (OPENCLAW_REFERENCE §8). In Phase 8,
memory/mnemosyne_provider.py implements this by wiring the real mnemosyne-memory
package (MNEMOSYNE_REFERENCE §6 shortest path). INTERFACE STUB — no logic yet.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """One active provider per process. Fail-open: errors never block a turn."""

    name: str = "base"

    @abstractmethod
    def initialize(self, session_id: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def system_prompt_block(self) -> str:
        """Static memory guidance injected into the system prompt."""

    @abstractmethod
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context before a turn (returns a context block)."""

    @abstractmethod
    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "", messages: list | None = None) -> None:
        """Persist a completed turn."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Memory tools exposed to the model (OpenAI function-calling format)."""

    @abstractmethod
    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str: ...

    def on_session_end(self) -> None:  # optional hook
        return None
