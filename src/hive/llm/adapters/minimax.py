"""
minimax.py — MiniMax adapter over the Anthropic-compatible Messages API.

Ported from the old Core/model_router._minimax. Uses MiniMax's Anthropic endpoint
so interleaved `thinking` blocks are preserved across turns (required for MiniMax
performance). Per-model quirks (whether thinking is supported, its budget) come
from the ModelCatalog, never hardcoded here.
"""
from __future__ import annotations

import httpx

from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
from hive.llm.model_catalog import ModelCatalog


class MiniMaxAdapter(LLMAdapter):
    name = "minimax"

    def __init__(
        self,
        base_url: str,
        catalog: ModelCatalog | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._catalog = catalog or ModelCatalog()
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        entry = self._catalog.get(request.model)
        body: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [m.to_dict() for m in request.messages],
            **request.extra,
        }
        if request.system:
            body["system"] = request.system
        if request.thinking and entry.supports_thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": entry.thinking_budget}

        r = await self._client.post(
            f"{self._base}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        # Concatenate text blocks; thinking blocks are not part of the visible reply.
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
        usage_raw = data.get("usage", {})
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        )
        return CompletionResult(
            text=text, model=request.model, usage=usage,
            finish_reason=data.get("stop_reason", "stop"), raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
