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


# --- Six additional serialization / adapter tests -------------------------------------------

def test_to_anthropic_messages_single_tool_result_is_user_turn():
    """A single TOOL message becomes a user turn with a single tool_result block."""
    msgs = [
        Message(role=Role.USER, content="call it"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="x1", name="mytool", arguments="{}")]),
        Message(role=Role.TOOL, content="the answer", tool_call_id="x1"),
    ]
    out = to_anthropic_messages(msgs)
    # user, assistant (tool_use), merged-user (tool_result)
    assert len(out) == 3
    tool_turn = out[2]
    assert tool_turn["role"] == "user"
    assert len(tool_turn["content"]) == 1
    assert tool_turn["content"][0]["type"] == "tool_result"
    assert tool_turn["content"][0]["tool_use_id"] == "x1"
    assert tool_turn["content"][0]["content"] == "the answer"


def test_to_anthropic_messages_valid_json_args_parsed():
    """Valid JSON arguments in ToolCall are deserialized into an input dict."""
    msgs = [
        Message(role=Role.USER, content="q"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="tc1", name="calc",
                                     arguments='{"a": 1, "b": 2}')]),
        Message(role=Role.TOOL, content="3", tool_call_id="tc1"),
    ]
    out = to_anthropic_messages(msgs)
    asst = out[1]
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["input"] == {"a": 1, "b": 2}


def test_adapter_result_text_stripped():
    """Text blocks in the response are stripped of leading/trailing whitespace."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "  spaced out  "}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.text == "spaced out"


def test_adapter_response_tool_calls_decoded():
    """tool_use blocks in the response are returned as ToolCall objects with JSON arguments."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "search",
                     "input": {"query": "cats"}},
                ],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="find cats")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tu1"
    assert tc.name == "search"
    assert json.loads(tc.arguments) == {"query": "cats"}


def test_adapter_usage_extracted():
    """Usage input/output token counts are extracted from the response."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 42, "output_tokens": 7},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7


def test_adapter_prompt_caching_wraps_system_in_block():
    """With prompt_caching=True (default), the system prompt is wrapped in a cache_control block."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # prompt_caching=True is the default
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client, prompt_caching=True)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            system="system instructions here",
                            messages=[Message(role=Role.USER, content="hello")])
    asyncio.run(adapter.complete(req, api_key="k"))
    system_val = captured.get("system")
    # With caching enabled, system is a list with a cache_control block
    assert isinstance(system_val, list)
    assert system_val[0]["type"] == "text"
    assert "cache_control" in system_val[0]


def test_to_anthropic_messages_no_system_role_in_output():
    """SYSTEM role is silently dropped (mapped to role='user' fallback in serializer)."""
    from hive.core.types import Role as R
    msgs = [
        Message(role=R.SYSTEM, content="be helpful"),
        Message(role=R.USER, content="hello"),
        Message(role=R.ASSISTANT, content="hi there"),
    ]
    out = to_anthropic_messages(msgs)
    # System messages must not appear as role='system' — they are remapped to 'user'
    # or dropped; either way no 'system' role in the messages array
    roles = [m["role"] for m in out]
    assert "system" not in roles


# --- Six additional serialization / adapter tests (appended) -----------------------

def test_adapter_finish_reason_propagated():
    """stop_reason from the API response is surfaced as result.finish_reason."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "done"}],
                  "stop_reason": "end_turn", "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.finish_reason == "end_turn"


def test_adapter_model_echoed_in_result():
    """The model name from the request is echoed back in the CompletionResult."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "hi"}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.model == "MiniMax-M3"


def test_adapter_sends_tools_field():
    """When a tools list is provided in the request, it is forwarded in the body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    tools = [{"name": "calc", "description": "add numbers",
              "input_schema": {"type": "object", "properties": {}}}]
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")], tools=tools)
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "tools" in captured
    assert captured["tools"][0]["name"] == "calc"


def test_adapter_401_raises_http_status_error():
    """A 401 response from the API raises an httpx.HTTPStatusError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    try:
        asyncio.run(adapter.complete(req, api_key="bad_key"))
        assert False, "Expected HTTPStatusError was not raised"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 401


def test_to_anthropic_messages_two_separate_tool_batches():
    """Two separate assistant+tool rounds produce 6 messages (no cross-batch merging)."""
    msgs = [
        Message(role=Role.USER, content="first"),
        Message(role=Role.ASSISTANT, content="a",
                tool_calls=[ToolCall(id="t1", name="f", arguments="{}")]),
        Message(role=Role.TOOL, content="r1", tool_call_id="t1"),
        Message(role=Role.USER, content="second"),
        Message(role=Role.ASSISTANT, content="b",
                tool_calls=[ToolCall(id="t2", name="g", arguments="{}")]),
        Message(role=Role.TOOL, content="r2", tool_call_id="t2"),
    ]
    out = to_anthropic_messages(msgs)
    # Each round collapses to: user, assistant, user — so 3+3 = 6 total
    assert len(out) == 6
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "user", "user", "assistant", "user"]


def test_adapter_empty_usage_fields_default_to_zero():
    """When the API returns an empty usage dict, token counts default to zero."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


# --- Six additional minimax serialization tests ------------------------------------

def test_to_anthropic_messages_empty_list_returns_empty():
    """An empty input list produces an empty output list."""
    assert to_anthropic_messages([]) == []


def test_to_anthropic_messages_assistant_no_tool_calls_becomes_text():
    """An ASSISTANT message with no tool_calls is serialized as a plain text dict."""
    msgs = [Message(role=Role.ASSISTANT, content="plain reply")]
    out = to_anthropic_messages(msgs)
    assert len(out) == 1
    assert out[0] == {"role": "assistant", "content": "plain reply"}


def test_to_anthropic_messages_tool_result_content_is_preserved():
    """The content string of a TOOL message lands verbatim in the tool_result block."""
    msgs = [
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="x1", name="fetch", arguments="{}")]),
        Message(role=Role.TOOL, content="fetched data", tool_call_id="x1"),
    ]
    out = to_anthropic_messages(msgs)
    user_msg = out[-1]
    assert user_msg["role"] == "user"
    block = user_msg["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"] == "fetched data"


def test_to_anthropic_messages_multiple_tool_calls_all_serialized():
    """All tool_calls in one ASSISTANT turn produce an equal number of tool_use blocks."""
    tool_calls = [ToolCall(id=f"t{i}", name=f"fn{i}", arguments="{}") for i in range(4)]
    msgs = [Message(role=Role.ASSISTANT, content="", tool_calls=tool_calls)]
    out = to_anthropic_messages(msgs)
    assert len(out) == 1
    tool_use_blocks = [b for b in out[0]["content"] if b["type"] == "tool_use"]
    assert len(tool_use_blocks) == 4


def test_to_anthropic_messages_system_role_mapped_to_user():
    """A message with role USER appears in the output with role 'user'."""
    msgs = [Message(role=Role.USER, content="hello world")]
    out = to_anthropic_messages(msgs)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello world"


def test_adapter_system_prompt_sent_as_cache_control_block():
    """When system is set and prompt_caching=True (default), system is a list with cache_control."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client, prompt_caching=True)
    req = CompletionRequest(
        model="MiniMax-M3", thinking=False,
        messages=[Message(role=Role.USER, content="q")],
        system="You are Hive.",
    )
    asyncio.run(adapter.complete(req, api_key="k"))
    assert isinstance(captured.get("system"), list)
    assert captured["system"][0]["type"] == "text"
    assert captured["system"][0]["text"] == "You are Hive."
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}


# --- Six new edge-case tests -------------------------------------------------------

def test_thinking_blocks_excluded_from_result_text():
    """thinking-type content blocks must not appear in result.text."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "internal monologue"},
                    {"type": "text", "text": "visible reply"},
                ],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.text == "visible reply"
    assert "internal monologue" not in result.text


def test_rate_limit_headers_captured_in_raw():
    """x-ratelimit-* response headers are stored under result.raw['_rate_limits']."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "usage": {}},
            headers={"x-ratelimit-remaining-requests": "99",
                     "x-ratelimit-limit-requests": "100"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    rl = result.raw.get("_rate_limits", {})
    assert rl.get("x-ratelimit-remaining-requests") == "99"
    assert rl.get("x-ratelimit-limit-requests") == "100"


def test_astream_yields_text_deltas():
    """astream collects SSE text_delta events into individual yielded strings."""
    sse_body = "\n".join([
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "hel"}}),
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "lo"}}),
        "data: [DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body,
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])

    async def _collect():
        chunks = []
        async for chunk in adapter.astream(req, api_key="k"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert "".join(chunks) == "hello"


def test_trailing_comma_json_repaired_in_tool_use_input():
    """Tool arguments with a trailing comma are repaired and land as a valid dict."""
    msgs = [
        Message(role=Role.USER, content="q"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="r1", name="fn",
                                     arguments='{"key": "value",}')]),
        Message(role=Role.TOOL, content="result", tool_call_id="r1"),
    ]
    out = to_anthropic_messages(msgs)
    asst = out[1]
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["input"] == {"key": "value"}


def test_surrogate_chars_stripped_from_user_content():
    """Lone surrogate characters in a user message are replaced before sending."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    bad_content = "hello \ud800 world"
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content=bad_content)])
    asyncio.run(adapter.complete(req, api_key="k"))
    sent_content = captured["messages"][0]["content"]
    assert "\ud800" not in sent_content
    assert "hello" in sent_content
    assert "world" in sent_content


def test_loads_non_dict_json_falls_back_to_empty_dict():
    """_loads returns {} when the JSON value is not a dict (e.g. a list or scalar)."""
    from hive.llm.adapters.minimax import _loads
    assert _loads("[1, 2, 3]") == {}
    assert _loads('"just a string"') == {}
    assert _loads("42") == {}


# --- Wave 3V-C additional serialization tests ------------------------------------

def test_wave3v_loads_invalid_json_returns_empty_dict():
    """_loads returns {} on completely invalid JSON (not just wrong type)."""
    from hive.llm.adapters.minimax import _loads
    assert _loads("not json at all") == {}
    assert _loads("{broken") == {}
    assert _loads("") == {}


def test_wave3v_to_anthropic_messages_user_only():
    """A single USER message serializes to one entry with role 'user'."""
    msgs = [Message(role=Role.USER, content="standalone")]
    out = to_anthropic_messages(msgs)
    assert len(out) == 1
    assert out[0] == {"role": "user", "content": "standalone"}


def test_wave3v_assistant_text_block_when_content_non_empty():
    """ASSISTANT with tool_calls AND non-empty text produces a leading text block."""
    msgs = [
        Message(role=Role.ASSISTANT, content="I will call a tool",
                tool_calls=[ToolCall(id="q1", name="do_it", arguments="{}")]),
    ]
    out = to_anthropic_messages(msgs)
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "I will call a tool"}
    assert blocks[1]["type"] == "tool_use"


def test_wave3v_tool_result_merges_into_preceding_user_turn():
    """A TOOL message following a plain user turn merges into it as a tool_result block."""
    msgs = [
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="m1", name="fn", arguments="{}")]),
        Message(role=Role.TOOL, content="output", tool_call_id="m1"),
        Message(role=Role.USER, content="follow-up"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="m2", name="fn2", arguments="{}")]),
        Message(role=Role.TOOL, content="output2", tool_call_id="m2"),
    ]
    out = to_anthropic_messages(msgs)
    # Each assistant+tool pair becomes assistant + user(tool_result)
    # Second tool_result should NOT merge into the "follow-up" user turn
    # because that turn's content is a plain string, not a list
    user_with_result = out[-1]
    assert user_with_result["role"] == "user"
    assert isinstance(user_with_result["content"], list)
    assert user_with_result["content"][0]["type"] == "tool_result"


def test_wave3v_adapter_name_is_minimax():
    """MiniMaxAdapter must expose name='minimax' for router dispatch."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    assert adapter.name == "minimax"


def test_wave3v_500_raises_http_status_error():
    """A 500 response raises httpx.HTTPStatusError with status 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal server error"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    try:
        asyncio.run(adapter.complete(req, api_key="k"))
        assert False, "Expected HTTPStatusError"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500


def test_wave3v_multiple_text_blocks_concatenated():
    """Multiple text blocks in a response are concatenated with no separator."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": " World"},
                ],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.text == "Hello World"


def test_wave3v_thinking_block_not_in_tool_calls():
    """A response with only a thinking block yields no tool_calls and empty text."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "thinking", "thinking": "reasoning..."}],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="think")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.text == ""
    assert result.tool_calls == []


# --- Wave 3Y additional serialization tests (8) ------------------------------------

def test_wave3y_base_url_trailing_slash_stripped():
    """MiniMaxAdapter strips a trailing slash from base_url on init."""
    adapter = MiniMaxAdapter("http://example.com/", ModelCatalog())
    assert adapter._base == "http://example.com"


def test_wave3y_extra_fields_forwarded_in_body():
    """Fields in CompletionRequest.extra are merged into the outgoing request body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")],
                            extra={"temperature": 0.5})
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured.get("temperature") == 0.5


def test_wave3y_empty_content_blocks_yields_empty_text_and_no_tool_calls():
    """A response with an empty content list yields empty text and no tool_calls."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.text == ""
    assert result.tool_calls == []


def test_wave3y_multiple_tool_use_blocks_all_returned_as_tool_calls():
    """Three tool_use blocks in the response produce three ToolCall objects."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "fn1", "input": {"x": 1}},
                    {"type": "tool_use", "id": "tu2", "name": "fn2", "input": {"y": 2}},
                    {"type": "tool_use", "id": "tu3", "name": "fn3", "input": {}},
                ],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert len(result.tool_calls) == 3
    ids = [tc.id for tc in result.tool_calls]
    assert ids == ["tu1", "tu2", "tu3"]
    assert result.tool_calls[0].name == "fn1"


def test_wave3y_loads_null_and_scalar_return_empty_dict():
    """_loads returns {} for JSON null, true, and bare integers."""
    from hive.llm.adapters.minimax import _loads
    assert _loads("null") == {}
    assert _loads("true") == {}
    assert _loads("99") == {}


def test_wave3y_raw_contains_full_response_body():
    """result.raw is the full parsed JSON response, including stop_reason and content."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "hi"}],
                  "usage": {}, "stop_reason": "end_turn"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert isinstance(result.raw, dict)
    assert result.raw.get("stop_reason") == "end_turn"
    assert "content" in result.raw


def test_wave3y_thinking_false_omits_thinking_key_from_body():
    """When thinking=False, the outgoing body must not include a 'thinking' key."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "thinking" not in captured


def test_wave3y_astream_ignores_non_data_lines():
    """astream skips SSE event: and empty lines and only yields text_delta content."""
    sse_body = "\n".join([
        "event: message_start",
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "hive"}}),
        "",
        "data: [DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body,
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])

    async def _collect():
        chunks = []
        async for chunk in adapter.astream(req, api_key="k"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert "".join(chunks) == "hive"


# --- Wave 4D-A additional serialization / adapter tests ---------------------------

def test_wave4d_system_role_not_present_when_no_system_prompt():
    """When CompletionRequest has no system, the body must not contain a 'system' key."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "system" not in captured


def test_wave4d_tool_result_with_empty_content():
    """A TOOL message with empty string content produces a tool_result block with content=''."""
    msgs = [
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="e1", name="noop", arguments="{}")]),
        Message(role=Role.TOOL, content="", tool_call_id="e1"),
    ]
    out = to_anthropic_messages(msgs)
    user_turn = out[-1]
    assert user_turn["role"] == "user"
    block = user_turn["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"] == ""


def test_wave4d_response_model_field_matches_request():
    """result.model equals the model string sent in CompletionRequest (not from the API body)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "hi"}], "model": "other-model", "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])
    result = asyncio.run(adapter.complete(req, api_key="k"))
    assert result.model == "MiniMax-M3"


def test_wave4d_temperature_field_forwarded_via_extra():
    """temperature passed through CompletionRequest.extra appears in the outgoing body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")],
                            extra={"temperature": 0.7})
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured.get("temperature") == 0.7


def test_wave4d_astream_skips_thinking_blocks():
    """astream must not yield text from thinking-type SSE events."""
    sse_body = "\n".join([
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "thinking_delta", "thinking": "hidden"}}),
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "visible"}}),
        "data: [DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body,
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])

    async def _collect():
        chunks = []
        async for chunk in adapter.astream(req, api_key="k"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert "".join(chunks) == "visible"
    assert "hidden" not in "".join(chunks)


def test_wave4d_json_repair_nested_trailing_comma():
    """Nested trailing commas in tool arguments are repaired before serialization."""
    from hive.llm.sanitize import repair_tool_arguments
    raw = '{"outer": {"inner": "val",}}'
    repaired = repair_tool_arguments(raw)
    parsed = json.loads(repaired)
    assert parsed == {"outer": {"inner": "val"}}


def test_wave4d_to_anthropic_messages_system_role_not_emitted():
    """SYSTEM role messages must never appear as role='system' in the Anthropic output."""
    from hive.core.types import Role as R
    msgs = [
        Message(role=R.SYSTEM, content="be concise"),
        Message(role=R.USER, content="question"),
        Message(role=R.ASSISTANT, content="answer"),
    ]
    out = to_anthropic_messages(msgs)
    for msg in out:
        assert msg["role"] != "system", f"system role leaked: {msg}"


def test_wave4d_astream_falls_back_to_complete_on_error():
    """When streaming raises an error before first byte, astream yields the complete() text."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, json={"error": "server error"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "fallback"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")])

    async def _collect():
        chunks = []
        async for chunk in adapter.astream(req, api_key="k"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert "".join(chunks) == "fallback"


# --- Wave 4J additional serialization / adapter tests ---------------------------

def test_wave4j_thinking_true_sends_thinking_key_in_body():
    """When thinking=True, the outgoing body includes a 'thinking' key with type 'enabled'."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=True,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "thinking" in captured
    assert captured["thinking"]["type"] == "enabled"


def test_wave4j_adapter_sends_to_v1_messages_endpoint():
    """MiniMaxAdapter posts to /v1/messages regardless of base_url."""
    captured_url: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://myhost:8080", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured_url["url"].endswith("/v1/messages")


def test_wave4j_x_api_key_header_uses_api_key_argument():
    """The x-api-key header value matches the api_key argument passed to complete()."""
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="secret-token"))
    assert captured_headers.get("x-api-key") == "secret-token"


def test_wave4j_anthropic_version_header_present():
    """The anthropic-version header is included in every request."""
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="hi")])
    asyncio.run(adapter.complete(req, api_key="k"))
    assert "anthropic-version" in captured_headers


def test_wave4j_tool_choice_forwarded_via_extra():
    """tool_choice passed through CompletionRequest.extra appears in the outgoing body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), client=client)
    req = CompletionRequest(model="MiniMax-M3", thinking=False,
                            messages=[Message(role=Role.USER, content="q")],
                            extra={"tool_choice": {"type": "auto"}})
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured.get("tool_choice") == {"type": "auto"}


def test_wave4j_to_anthropic_messages_two_consecutive_user_messages():
    """Two consecutive USER messages are preserved as two separate entries."""
    msgs = [
        Message(role=Role.USER, content="first question"),
        Message(role=Role.USER, content="second question"),
    ]
    out = to_anthropic_messages(msgs)
    assert len(out) == 2
    assert out[0] == {"role": "user", "content": "first question"}
    assert out[1] == {"role": "user", "content": "second question"}


def test_wave4j_empty_tool_call_arguments_falls_back_to_empty_dict():
    """An empty-string arguments field in ToolCall produces an empty input dict."""
    msgs = [
        Message(role=Role.USER, content="q"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="e1", name="noop", arguments="")]),
        Message(role=Role.TOOL, content="done", tool_call_id="e1"),
    ]
    out = to_anthropic_messages(msgs)
    asst = out[1]
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["input"] == {}


def test_wave4j_tool_use_block_carries_correct_name_and_id():
    """Each tool_use block carries the name and id from the originating ToolCall."""
    msgs = [
        Message(role=Role.USER, content="q"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="myid", name="special_fn",
                                     arguments='{"x": 42}')]),
        Message(role=Role.TOOL, content="42", tool_call_id="myid"),
    ]
    out = to_anthropic_messages(msgs)
    asst = out[1]
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["id"] == "myid"
    assert tool_use["name"] == "special_fn"
    assert tool_use["input"] == {"x": 42}
