"""
anthropic.py — native Anthropic Messages API adapter (M8 B1).

MiniMax already speaks the Anthropic Messages wire format, so the native Anthropic
provider is the same adapter pointed at api.anthropic.com — it reuses MiniMaxAdapter's
message serialization, sanitization, prompt-cache, SSE streaming, and rate-limit parsing
unchanged. This is the provider-plugin payoff: a second real executor with ~zero new code.

Auth: the shared `_headers` already sends `x-api-key` + `anthropic-version`, which is
exactly Anthropic's scheme — the router's credential pool supplies the Anthropic key.
"""
from __future__ import annotations

import httpx

from hive.llm.adapters.minimax import MiniMaxAdapter
from hive.llm.model_catalog import ModelCatalog


class AnthropicAdapter(MiniMaxAdapter):
    name = "anthropic"

    def __init__(self, base_url: str = "https://api.anthropic.com",
                 catalog: ModelCatalog | None = None, *,
                 client: httpx.AsyncClient | None = None, timeout: float = 300.0,
                 prompt_caching: bool = True) -> None:
        super().__init__(base_url, catalog, client=client, timeout=timeout,
                         prompt_caching=prompt_caching)
