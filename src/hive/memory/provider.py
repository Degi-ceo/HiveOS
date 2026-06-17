"""
provider.py — MemoryProvider ABC (single active memory slot).

Contract combines Hermes's MemoryProvider lifecycle (HERMES_REFERENCE §6) with
OpenClaw's single-active-memory-slot rule (OPENCLAW_REFERENCE §8). In Phase 8,
memory/mnemosyne_provider.py implements this by wiring the real mnemosyne-memory
package (MNEMOSYNE_REFERENCE §6 shortest path). The active provider (LocalMemoryProvider, HiveMnemosyneProvider) implements this.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict


class MemoryEntry(TypedDict):
    """Type-safe representation of a knowledge entry."""
    kind: str
    topic: str
    content: str
    source: str


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

    def recent(self, session: str = "", limit: int = 30) -> list[dict]:
        """Return recent episodic turns for a session (optional, provider-specific).

        Providers without a local episodic index return []. Callers that need a
        portable context string should prefer prefetch() which is always in the ABC.
        """
        return []

    # Optional richer surface implemented by LocalMemoryProvider and, where possible,
    # adapters. Defaults are fail-open so diagnostics/gateway endpoints can call the
    # active provider through the base contract without fragile hasattr branches.
    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        return None

    def already_known(self, topic: str) -> bool:
        return bool(self.recall(topic, limit=1))

    def count(self) -> dict[str, int]:
        return {}

    def count_episodic(self, session_id: str) -> int:
        return 0

    def delete_session_memory(self, session_id: str) -> int:
        return 0

    def list_topics(self, kind: str | None = None) -> list[str]:
        return []

    def wipe_knowledge(self, kind: str | None = None) -> int:
        return 0

    def memory_stats(self) -> dict[str, Any]:
        return {"knowledge_count": 0, "episodic_count": 0, "avg_importance": 0.0,
                "oldest_ts": None, "newest_ts": None, "by_kind": {}}

    def most_important_facts(self, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def export_backup(self) -> dict[str, Any]:
        return {"knowledge": [], "episodic": [],
                "knowledge_count": 0, "episodic_count": 0}
