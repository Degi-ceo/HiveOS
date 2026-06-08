"""
minimax.py — MiniMax adapter over the Anthropic-compatible Messages API.

Ported from the old Core/model_router._minimax. Uses MiniMax's Anthropic endpoint
so interleaved `thinking` blocks are preserved across turns (required for MiniMax
performance). Per-model quirks (whether thinking is supported, its budget) come
from the ModelCatalog, never hardcoded here.
"""
from __future__ import annotations

import json

import httpx

from hive.core.types import Message, Role, ToolCall
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
from hive.llm.model_catalog import ModelCatalog


def _loads(arguments: str) -> dict:
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def to_anthropic_messages(messages: list[Message]) -> list[dict]:
    """Serialize canonical Messages to the Anthropic Messages API shape.

    The core uses an OpenAI-flavored Message (Message.to_dict); MiniMax's endpoint is
    Anthropic, which needs tool turns as content blocks: assistant tool_calls ->
    `tool_use`, a TOOL message -> a user `tool_result`. Consecutive tool results are
    merged into one user turn (Anthropic requires them in a single user message).
    """
    out: list[dict] = []
    for m in messages:
        if m.role is Role.TOOL:
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id or "",
                     "content": m.content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m.role is Role.ASSISTANT and m.tool_calls:
            content: list[dict] = []
            if m.content:
                content.append({"type": "text", "text": m.content})
            content.extend({"type": "tool_use", "id": tc.id, "name": tc.name,
                            "input": _loads(tc.arguments)} for tc in m.tool_calls)
            out.append({"role": "assistant", "content": content})
        else:
            # USER/ASSISTANT plain text (SYSTEM is passed via the `system` param).
            role = "assistant" if m.role is Role.ASSISTANT else "user"
            out.append({"role": role, "content": m.content})
    return out


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
            "messages": to_anthropic_messages(request.messages),
            **request.extra,
        }
        if request.system:
            body["system"] = request.system
        if request.tools:
            body["tools"] = request.tools
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
        blocks = data.get("content", [])
        # Concatenate text blocks; thinking blocks are not part of the visible reply.
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        # Map Anthropic tool_use blocks to canonical ToolCall (arguments JSON-encoded).
        tool_calls = [
            ToolCall(id=b.get("id", ""), name=b.get("name", ""),
                     arguments=json.dumps(b.get("input", {})))
            for b in blocks if b.get("type") == "tool_use"
        ]
        usage_raw = data.get("usage", {})
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        )
        return CompletionResult(
            text=text, model=request.model, usage=usage,
            finish_reason=data.get("stop_reason", "stop"), tool_calls=tool_calls, raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
