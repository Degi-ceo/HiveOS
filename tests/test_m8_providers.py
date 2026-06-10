"""M8 — provider flexibility: anthropic + codex adapters, provider registry, selection."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from hive.core.config import HiveConfig
from hive.core.types import Message, Role
from hive.llm.adapters import make_adapter, PROVIDERS
from hive.llm.adapters.base import CompletionRequest


# --- provider registry ---------------------------------------------------------

def test_make_adapter_known_providers():
    from hive.llm.adapters.minimax import MiniMaxAdapter
    from hive.llm.adapters.anthropic import AnthropicAdapter
    from hive.llm.adapters.codex import CodexAdapter
    assert isinstance(make_adapter("minimax"), MiniMaxAdapter)
    a = make_adapter("anthropic")
    assert isinstance(a, AnthropicAdapter) and a.name == "anthropic"
    assert isinstance(make_adapter("codex"), CodexAdapter)
    assert set(PROVIDERS) == {"minimax", "anthropic", "codex"}


def test_make_adapter_unknown_raises():
    with pytest.raises(ValueError):
        make_adapter("gpt-9")


# --- codex adapter (shared subprocess core) ------------------------------------

def test_render_prompt():
    from hive.llm.adapters.codex import render_prompt
    out = render_prompt([Message(role=Role.USER, content="hi")], "ctx")
    assert "[CONTEXT]" in out and "ctx" in out and "user: hi" in out


def test_codex_adapter_complete_via_cat():
    from hive.llm.adapters.codex import CodexAdapter
    adapter = CodexAdapter(cmd="cat")  # echoes stdin
    req = CompletionRequest(model="codex", messages=[Message(role=Role.USER, content="plan x")])
    res = asyncio.run(adapter.complete(req, api_key=""))
    assert res.model == "codex" and "plan x" in res.text


def test_codex_adapter_missing_binary_raises():
    from hive.llm.adapters.codex import CodexAdapter, PlannerError
    adapter = CodexAdapter(cmd="definitely-not-real-bin-xyz")
    req = CompletionRequest(model="codex", messages=[Message(role=Role.USER, content="x")])
    with pytest.raises(PlannerError):
        asyncio.run(adapter.complete(req, api_key=""))


def test_make_codex_planner_still_works_after_refactor():
    from hive.llm.router import make_codex_planner, PlannerError
    planner = make_codex_planner("cat", timeout=10)
    out = asyncio.run(planner([Message(role=Role.USER, content="hello")], None))
    assert "hello" in out
    with pytest.raises(PlannerError):
        asyncio.run(make_codex_planner("false")([Message(role=Role.USER, content="x")], None))


# --- anthropic adapter (native Anthropic wire) ---------------------------------

def test_anthropic_adapter_calls_messages_endpoint():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi from claude"}],
                                         "usage": {"input_tokens": 3, "output_tokens": 2},
                                         "stop_reason": "end_turn"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter("https://api.anthropic.com", client=client)
    req = CompletionRequest(model="claude-x", messages=[Message(role=Role.USER, content="yo")])
    res = asyncio.run(adapter.complete(req, api_key="ak-123"))
    assert res.text == "hi from claude" and res.usage.output_tokens == 2
    assert seen["url"].endswith("/v1/messages") and seen["key"] == "ak-123"
    assert seen["version"]  # anthropic-version header present


# --- runtime provider selection ------------------------------------------------

def test_runtime_selects_anthropic_provider(tmp_path, monkeypatch):
    from hive.runtime import HiveOS
    monkeypatch.setenv("HIVE_EXEC_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak1,ak2")
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False))  # real router
    assert h.router._adapter.name == "anthropic"
    assert len(h.router._pool) == 2          # multi-key from ANTHROPIC_API_KEY
    asyncio.run(h.aclose())


def test_runtime_defaults_to_minimax(tmp_path, monkeypatch):
    from hive.runtime import HiveOS
    monkeypatch.setenv("MINIMAX_API_KEY", "mk")
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False))
    assert h.router._adapter.name == "minimax"
    asyncio.run(h.aclose())
