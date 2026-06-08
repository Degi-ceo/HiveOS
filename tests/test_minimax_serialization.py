"""Regression: MiniMax adapter must serialize tool turns to Anthropic format."""
from __future__ import annotations

import asyncio
import json

import httpx

from hive.core.types import Message, Role, ToolCall
from hive.llm.adapters.base import CompletionRequest
from hive.llm.adapters.minimax import MiniMaxAdapter, to_anthropic_messages
from hive.llm.model_catalog import ModelCatalog


def test_to_anthropic_messages_tool_turn():
    msgs = [
        Message(role=Role.USER, content="hi"),
        Message(role=Role.ASSISTANT, content="thinking",
                tool_calls=[ToolCall(id="c1", name="echo", arguments='{"t": 1}'),
                            ToolCall(id="c2", name="ls", arguments="not-json")]),
        Message(role=Role.TOOL, content="echoed", tool_call_id="c1"),
        Message(role=Role.TOOL, content="listed", tool_call_id="c2"),
    ]
    out = to_anthropic_messages(msgs)
    assert out[0] == {"role": "user", "content": "hi"}
    # assistant tool_calls -> text + tool_use blocks (bad JSON args degrade to {})
    assert out[1]["role"] == "assistant"
    kinds = [b["type"] for b in out[1]["content"]]
    assert kinds == ["text", "tool_use", "tool_use"]
    assert out[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "echo", "input": {"t": 1}}
    assert out[1]["content"][2]["input"] == {}
    # both tool results merged into ONE user turn (Anthropic requirement)
    assert len(out) == 3
    assert out[2]["role"] == "user"
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "tool_result"]
    assert out[2]["content"][0]["tool_use_id"] == "c1"


def test_adapter_sends_anthropic_body_over_the_wire():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(
        model="MiniMax-M3", thinking=False,
        messages=[Message(role=Role.ASSISTANT, content="",
                          tool_calls=[ToolCall(id="c1", name="echo", arguments="{}")]),
                  Message(role=Role.TOOL, content="r", tool_call_id="c1")])
    out = asyncio.run(adapter.complete(req, api_key="k"))
    assert out.text == "ok"
    sent = captured["messages"]
    # never leak OpenAI-style tool_calls / role:"tool" to an Anthropic endpoint
    assert all("tool_calls" not in m for m in sent)
    assert all(m["role"] in ("user", "assistant") for m in sent)
    assert sent[0]["content"][0]["type"] == "tool_use"
    assert sent[1]["content"][0]["type"] == "tool_result"
