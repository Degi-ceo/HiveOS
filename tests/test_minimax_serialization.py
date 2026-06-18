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


# --- MiniMax serialization additional cases -------------------------------------------

def test_to_anthropic_messages_plain_user_assistant_pair():
    """Simple user+assistant pair without tool calls passes through unchanged."""
    msgs = [
        Message(role=Role.USER, content="What is the capital of France?"),
        Message(role=Role.ASSISTANT, content="Paris."),
    ]
    out = to_anthropic_messages(msgs)
    assert len(out) == 2
    assert out[0] == {"role": "user", "content": "What is the capital of France?"}
    assert out[1]["role"] == "assistant"


def test_to_anthropic_messages_empty_assistant_tool_calls():
    """Assistant with no text and only a tool call produces text+tool_use blocks."""
    msgs = [
        Message(role=Role.USER, content="get data"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="t1", name="fetch", arguments='{"url": "x"}')]),
        Message(role=Role.TOOL, content="data result", tool_call_id="t1"),
    ]
    out = to_anthropic_messages(msgs)
    # assistant has text block (empty) + tool_use block
    asst = out[1]
    assert asst["role"] == "assistant"
    block_types = [b["type"] for b in asst["content"]]
    assert "tool_use" in block_types
    # tool result is a user turn
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"


def test_adapter_body_contains_model_field():
    """The Anthropic-format body sent by MiniMaxAdapter must include the model field."""
    import json
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    import httpx
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured.get("model") == "MiniMax-M3"


# --- Additional serialization tests -------------------------------------------

def test_to_anthropic_messages_system_message_excluded():
    """SYSTEM role messages are not emitted as role='system' in the output list.

    The Anthropic Messages API only accepts 'user' and 'assistant' in the messages
    array; system belongs at the top-level 'system' field.  to_anthropic_messages
    must not produce any message whose role is 'system'.
    """
    from hive.core.types import Role as R
    msgs = [
        Message(role=R.SYSTEM, content="you are helpful"),
        Message(role=R.USER, content="hello"),
    ]
    out = to_anthropic_messages(msgs)
    roles = [m["role"] for m in out]
    # No message must carry role='system' — that would be rejected by the API
    assert "system" not in roles


def test_to_anthropic_messages_multiple_tool_results_merged():
    """Multiple consecutive TOOL turns collapse into a single user turn."""
    msgs = [
        Message(role=Role.USER, content="go"),
        Message(role=Role.ASSISTANT, content="working",
                tool_calls=[
                    ToolCall(id="t1", name="a", arguments="{}"),
                    ToolCall(id="t2", name="b", arguments="{}"),
                ]),
        Message(role=Role.TOOL, content="result-a", tool_call_id="t1"),
        Message(role=Role.TOOL, content="result-b", tool_call_id="t2"),
    ]
    out = to_anthropic_messages(msgs)
    # There should be exactly 3 entries: user, assistant, merged-user
    assert len(out) == 3
    merged = out[2]
    assert merged["role"] == "user"
    assert len(merged["content"]) == 2
    assert all(b["type"] == "tool_result" for b in merged["content"])


def test_adapter_sends_max_tokens_field():
    """The request body contains a 'max_tokens' key when the request specifies one."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "x"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False, max_tokens=512,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "max_tokens" in captured


def test_to_anthropic_messages_assistant_with_only_text():
    """An assistant message with no tool calls serializes to a plain string content."""
    msgs = [
        Message(role=Role.USER, content="ping"),
        Message(role=Role.ASSISTANT, content="pong"),
    ]
    out = to_anthropic_messages(msgs)
    asst = out[1]
    assert asst["role"] == "assistant"
    # No tool calls → content is a plain string (not a list of blocks)
    assert asst["content"] == "pong"


def test_to_anthropic_messages_preserves_order():
    """Messages come out in the same order they were provided."""
    msgs = [
        Message(role=Role.USER, content="first"),
        Message(role=Role.ASSISTANT, content="second"),
        Message(role=Role.USER, content="third"),
    ]
    out = to_anthropic_messages(msgs)
    assert len(out) == 3
    assert out[0]["content"] == "first"
    assert out[2]["content"] == "third"


def test_adapter_sends_system_field():
    """When a system prompt is provided, the body includes a top-level 'system' field."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Disable prompt caching so system is a plain string, not a list of blocks
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client, prompt_caching=False)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            system="you are a helpful assistant",
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "system" in captured
    assert "helpful assistant" in captured["system"]
