"""
hive.llm.adapters — provider adapters + a small provider-plugin registry (M8).

`make_adapter(provider, ...)` is the one place that maps a provider name to an
LLMAdapter, so the runtime can switch the executor (minimax ↔ anthropic) from config
without touching wiring. Codex is the planner provider (subprocess, no api_key).
Imports are lazy so importing this package doesn't pull every adapter (and httpx).
"""
from __future__ import annotations

from hive.llm.adapters.base import LLMAdapter

# Provider names HiveOS knows. "codex" is planner-only (no executor/credential pool).
PROVIDERS = ("minimax", "anthropic", "codex")


def make_adapter(provider: str, *, base_url: str = "",
                 catalog: object | None = None) -> LLMAdapter:
    """Build an LLMAdapter for a provider name. Raises ValueError for unknown ones."""
    p = (provider or "minimax").lower()
    if p == "minimax":
        from hive.llm.adapters.minimax import MiniMaxAdapter
        return MiniMaxAdapter(base_url or "https://api.minimax.io/anthropic", catalog)  # type: ignore[arg-type]
    if p == "anthropic":
        from hive.llm.adapters.anthropic import AnthropicAdapter
        return AnthropicAdapter(base_url or "https://api.anthropic.com", catalog)  # type: ignore[arg-type]
    if p == "codex":
        from hive.llm.adapters.codex import CodexAdapter
        return CodexAdapter()
    raise ValueError(f"unknown provider: {provider!r} (known: {', '.join(PROVIDERS)})")
