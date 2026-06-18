"""Tests for LLM adapter internals: prompt caching, aclose, AnthropicAdapter config."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hive.core.types import Message, Role
from hive.llm.adapters.base import CompletionRequest
from hive.llm.adapters.minimax import MiniMaxAdapter
from hive.llm.model_catalog import ModelCatalog


def _req(system: str | None = None) -> CompletionRequest:
    return CompletionRequest(
        model="MiniMax-M3",
        messages=[Message(role=Role.USER, content="hi")],
        system=system,
        thinking=False,
    )


# --- Task 1: _build_body() prompt caching ----------------------------------------

def test_minimax_build_body_with_caching_adds_cache_control():
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), prompt_caching=True)
    body = adapter._build_body(_req(system="Be helpful."))
    system = body["system"]
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "Be helpful."
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_minimax_build_body_without_caching_no_cache_control():
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), prompt_caching=False)
    body = adapter._build_body(_req(system="Be helpful."))
    assert body["system"] == "Be helpful."


def test_minimax_build_body_no_system_field_when_system_is_none():
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), prompt_caching=True)
    body = adapter._build_body(_req(system=None))
    assert "system" not in body


# --- Task 2: MiniMaxAdapter.aclose() ---------------------------------------------

def test_minimax_adapter_aclose():
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    asyncio.run(adapter.aclose())
    assert adapter._client.is_closed


def test_minimax_adapter_aclose_with_injected_client():
    client = httpx.AsyncClient()
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    asyncio.run(adapter.aclose())
    assert client.is_closed


# --- Task 3: AnthropicAdapter instantiation and config ---------------------------

def test_anthropic_adapter_default_base_url():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    assert "api.anthropic.com" in adapter._base


def test_anthropic_adapter_custom_base_url():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter(base_url="https://custom.example.com")
    assert adapter._base == "https://custom.example.com"


def test_anthropic_adapter_name():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    assert AnthropicAdapter.name == "anthropic"


def test_anthropic_adapter_prompt_caching_default_on():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    assert adapter._prompt_caching is True


def test_anthropic_adapter_aclose():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    asyncio.run(adapter.aclose())
    assert adapter._client.is_closed


# --- Task 4: astream() fallback on error ----------------------------------------

def test_minimax_astream_fallback_on_error(monkeypatch):
    """astream() falls back to complete() when the SSE stream raises."""
    complete_calls: list[str] = []

    async def fake_complete(request, *, api_key):
        from hive.llm.adapters.base import CompletionResult
        complete_calls.append(api_key)
        return CompletionResult(text="fallback text", model=request.model)

    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    monkeypatch.setattr(adapter, "complete", fake_complete)

    # Patch _client.stream to raise immediately, simulating SSE failure.
    class _FailStream:
        async def __aenter__(self):
            raise httpx.ConnectError("connection refused")

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(adapter._client, "stream", lambda *a, **kw: _FailStream())

    async def collect():
        chunks = []
        async for chunk in adapter.astream(_req(), api_key="test-key"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert chunks == ["fallback text"]
    assert complete_calls == ["test-key"]
