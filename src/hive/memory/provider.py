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

from hive.core.types import ContentTrust


class MemoryEntry(TypedDict):
    """Type-safe representation of a knowledge entry."""
    kind: str
    topic: str
    content: str
    source: str
    trust: str
    importance: float
    superseded_by: int | str | None


def learn_with_provenance(
    provider: Any,
    kind: str,
    topic: str,
    content: str,
    source: str = "",
    *,
    trust: ContentTrust,
    importance: float,
    supersede: bool = False,
) -> int | str | None:
    """Persist provenance while retaining compatibility with legacy adapters.

    Third-party providers and older injected test doubles may still implement
    the pre-M2 ``learn(kind, topic, content, source)`` signature. Only that
    specific signature mismatch falls back; provider-internal TypeErrors still
    propagate so real persistence defects are never hidden.
    """
    try:
        return provider.learn(
            kind,
            topic,
            content,
            source,
            trust=trust,
            importance=importance,
            supersede=supersede,
        )
    except TypeError as exc:
        message = str(exc)
        if (
            "unexpected keyword argument" not in message
            or not any(name in message for name in ("trust", "importance", "supersede"))
        ):
            raise
        return provider.learn(kind, topic, content, source)


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
    def recall(self, query: str, limit: int = 5, *,
               trusted_only: bool = False) -> list[dict[str, Any]]:
        return []

    def learn(self, kind: str, topic: str, content: str, source: str = "", *,
              trust: ContentTrust = ContentTrust.UNTRUSTED,
              importance: float = 0.5, supersede: bool = False) -> int | str | None:
        return None

    def already_known(self, topic: str, *, content: str | None = None) -> bool:
        hits = self.recall(topic, limit=20)
        return any(content is None or hit.get("content") == content for hit in hits)

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

    def most_important_facts(self, limit: int = 10, *,
                             trusted_only: bool = False) -> list[dict[str, Any]]:
        return []

    def export_backup(self) -> dict[str, Any]:
        return {"knowledge": [], "episodic": [],
                "knowledge_count": 0, "episodic_count": 0}
