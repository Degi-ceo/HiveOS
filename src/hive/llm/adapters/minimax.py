"""
minimax.py — MiniMax adapter over the Anthropic-compatible Messages API.

Ported from the old Core/model_router._minimax. Uses MiniMax's Anthropic endpoint
so interleaved `thinking` blocks are preserved across turns (required for MiniMax
performance). Per-model quirks (whether thinking is supported, its budget) come
from the ModelCatalog, never hardcoded here.

Added (HERMES_REFERENCE):
- Message sanitization (surrogate stripping, tool-arg JSON repair) before send.
- Anthropic prompt-caching: system prompt wrapped in a cache_control block when
  the model catalog entry opts in (`prompt_caching=True`). Saves tokens on
  repeated turns where SOUL.md + memory block is the stable prefix.
- Rate-limit header capture: x-ratelimit-* headers stored in CompletionResult.raw
  so the router/budgeter can read reset windows without a separate API call.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from hive.core.types import Message, Role, ToolCall
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
from hive.llm.model_catalog import ModelCatalog
from hive.llm.sanitize import repair_tool_arguments, sanitize_messages


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
                            "input": _loads(repair_tool_arguments(tc.arguments))}
                           for tc in m.tool_calls)
            out.append({"role": "assistant", "content": content})
        else:
            role = "assistant" if m.role is Role.ASSISTANT else "user"
            out.append({"role": role, "content": m.content})
    return out


def _extract_rate_limit_headers(headers: httpx.Headers) -> dict[str, Any]:
    """Capture x-ratelimit-* headers for the router/budgeter to read."""
    relevant = (
        "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    )
    return {k: headers[k] for k in relevant if k in headers}


class MiniMaxAdapter(LLMAdapter):
    name = "minimax"

    def __init__(
        self,
        base_url: str,
        catalog: ModelCatalog | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        prompt_caching: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._catalog = catalog or ModelCatalog()
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._prompt_caching = prompt_caching

    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        entry = self._catalog.get(request.model)
        # Build and sanitize message list (surrogate strip + tool-arg repair).
        raw_messages = sanitize_messages(to_anthropic_messages(request.messages))
        body: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": raw_messages,
            **request.extra,
        }
        if request.system:
            if self._prompt_caching:
                # Wrap the system prompt in a cache_control block so Anthropic
                # caches the stable SOUL+memory prefix across turns.
                body["system"] = [
                    {"type": "text", "text": request.system,
                     "cache_control": {"type": "ephemeral"}}
                ]
            else:
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
        # Capture rate-limit headers for the router (merged into raw dict).
        rate_limit_info = _extract_rate_limit_headers(r.headers)
        if rate_limit_info:
            data["_rate_limits"] = rate_limit_info

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
