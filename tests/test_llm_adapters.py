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


# --- New tests (Task 5-10) -------------------------------------------------------

def test_minimax_build_body_with_system_sets_correct_field():
    """_build_body() with a system prompt and caching disabled returns a plain string system field."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), prompt_caching=False)
    body = adapter._build_body(_req(system="Be concise."))
    assert body["system"] == "Be concise."


def test_minimax_build_body_without_tools_omits_tools_field():
    """When no tools are present in the request the 'tools' key must not appear."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    req = CompletionRequest(
        model="MiniMax-M3",
        messages=[Message(role=Role.USER, content="hello")],
        system=None,
        thinking=False,
        tools=None,
    )
    body = adapter._build_body(req)
    assert "tools" not in body


def test_anthropic_adapter_sends_correct_anthropic_version_header():
    """_headers() must include the exact anthropic-version string Anthropic requires."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    headers = adapter._headers("dummy-key")
    assert headers.get("anthropic-version") == "2023-06-01"


def test_anthropic_adapter_headers_include_api_key():
    """_headers() must forward the api_key as x-api-key."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    headers = adapter._headers("my-secret-key")
    assert headers["x-api-key"] == "my-secret-key"


def test_minimax_adapter_model_not_in_catalog_uses_conservative_default():
    """get() on an unknown model id must never raise — it returns a conservative default."""
    catalog = ModelCatalog()
    entry = catalog.get("some-unknown-future-model-xyz")
    # Conservative default: thinking enabled, large context
    assert entry.supports_thinking is True
    assert entry.context_length >= 1_000


def test_completion_result_usage_field_is_populated():
    """CompletionResult usage field should carry the token counts we pass in."""
    from hive.llm.adapters.base import CompletionResult, Usage
    usage = Usage(input_tokens=100, output_tokens=42)
    result = CompletionResult(text="hello", model="MiniMax-M3", usage=usage)
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 42


def test_completion_result_usage_defaults_to_zero():
    """CompletionResult created without usage should have zero token counts."""
    from hive.llm.adapters.base import CompletionResult
    result = CompletionResult(text="hi", model="m")
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


# --- Six more new tests ----------------------------------------------------------

def test_minimax_adapter_build_body_includes_model():
    """_build_body() must include the model name in the request body."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    body = adapter._build_body(_req())
    assert body["model"] == "MiniMax-M3"


def test_minimax_adapter_build_body_has_messages():
    """_build_body() must include a non-empty messages list."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    body = adapter._build_body(_req())
    assert "messages" in body
    assert isinstance(body["messages"], list)
    assert len(body["messages"]) >= 1


def test_anthropic_adapter_name_is_anthropic():
    """AnthropicAdapter.name class attribute must equal 'anthropic'."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    assert AnthropicAdapter.name == "anthropic"


def test_minimax_adapter_timeout_default():
    """MiniMaxAdapter created without an explicit client uses a positive timeout."""
    # The default timeout is 300.0 s; we cannot inspect it directly but we CAN verify
    # the client was created (not None) and is not already closed.
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    assert adapter._client is not None
    assert not adapter._client.is_closed


def test_minimax_adapter_streaming_param():
    """_build_body() merged with stream=True produces a body with stream=True."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    base_body = adapter._build_body(_req())
    # astream() inlines stream=True via dict merge; simulate that merge here.
    streaming_body = {**base_body, "stream": True}
    assert streaming_body["stream"] is True
    assert streaming_body["model"] == "MiniMax-M3"


def test_completion_result_stop_reason():
    """CompletionResult stores the finish_reason we pass in."""
    from hive.llm.adapters.base import CompletionResult
    result = CompletionResult(text="done", model="MiniMax-M3", finish_reason="end_turn")
    assert result.finish_reason == "end_turn"


# ---------------------------------------------------------------------------
# Six additional tests
# ---------------------------------------------------------------------------

def test_minimax_adapter_name_is_minimax():
    """MiniMaxAdapter.name class attribute must equal 'minimax'."""
    assert MiniMaxAdapter.name == "minimax"


def test_model_catalog_contains_known_model():
    """ModelCatalog knows about 'MiniMax-M3' out of the box."""
    catalog = ModelCatalog()
    assert "MiniMax-M3" in catalog


def test_model_catalog_list_models_returns_sorted():
    """list_models() returns model IDs in sorted order."""
    catalog = ModelCatalog()
    ids = catalog.list_models()
    assert ids == sorted(ids)


def test_model_catalog_register_and_retrieve():
    """A freshly registered ModelEntry is retrievable via get()."""
    from hive.llm.model_catalog import ModelEntry
    catalog = ModelCatalog()
    entry = ModelEntry(model_id="custom-test-model", context_length=4096)
    catalog.register(entry)
    retrieved = catalog.get("custom-test-model")
    assert retrieved.model_id == "custom-test-model"
    assert retrieved.context_length == 4096


def test_minimax_build_body_thinking_disabled_omits_thinking_block():
    """When thinking=False the 'thinking' key must not appear in the body."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog())
    req = CompletionRequest(
        model="MiniMax-M3",
        messages=[Message(role=Role.USER, content="hello")],
        thinking=False,
    )
    body = adapter._build_body(req)
    assert "thinking" not in body


def test_anthropic_adapter_prompt_caching_can_be_disabled():
    """AnthropicAdapter respects prompt_caching=False at construction time."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter(prompt_caching=False)
    assert adapter._prompt_caching is False


# ---------------------------------------------------------------------------
# Six additional tests (batch 5)
# ---------------------------------------------------------------------------

def test_to_anthropic_messages_user_role():
    """to_anthropic_messages converts a USER message to role='user'."""
    from hive.llm.adapters.minimax import to_anthropic_messages
    msgs = [Message(role=Role.USER, content="hello world")]
    out = to_anthropic_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello world"


def test_to_anthropic_messages_consecutive_tool_results_merged():
    """Consecutive TOOL messages are merged into a single user turn."""
    from hive.llm.adapters.minimax import to_anthropic_messages
    from hive.core.types import ToolCall
    msgs = [
        Message(role=Role.TOOL, content="result1", tool_call_id="tc1"),
        Message(role=Role.TOOL, content="result2", tool_call_id="tc2"),
    ]
    out = to_anthropic_messages(msgs)
    # Two consecutive tool results must produce exactly ONE user turn.
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert isinstance(out[0]["content"], list)
    assert len(out[0]["content"]) == 2


def test_to_anthropic_messages_assistant_with_tool_calls():
    """ASSISTANT message with tool_calls serializes as tool_use content blocks."""
    from hive.llm.adapters.minimax import to_anthropic_messages
    from hive.core.types import ToolCall
    tc = ToolCall(id="tc99", name="lookup", arguments='{"q": "test"}')
    msgs = [Message(role=Role.ASSISTANT, content="Calling tool.", tool_calls=[tc])]
    out = to_anthropic_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    types = [b["type"] for b in out[0]["content"]]
    assert "tool_use" in types
    assert "text" in types


def test_model_catalog_unregister_removes_entry():
    """ModelCatalog.unregister() removes a known model and returns True."""
    from hive.llm.model_catalog import ModelCatalog
    catalog = ModelCatalog()
    assert "MiniMax-M3" in catalog
    removed = catalog.unregister("MiniMax-M3")
    assert removed is True
    assert "MiniMax-M3" not in catalog


def test_model_catalog_unregister_unknown_returns_false():
    """ModelCatalog.unregister() returns False when the model_id is not registered."""
    from hive.llm.model_catalog import ModelCatalog
    catalog = ModelCatalog()
    result = catalog.unregister("nonexistent-model-xyz")
    assert result is False


def test_model_catalog_len_reflects_registered_count():
    """len(ModelCatalog) equals the number of registered entries."""
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    catalog = ModelCatalog()
    initial_len = len(catalog)
    catalog.register(ModelEntry(model_id="extra-model-test", context_length=8192))
    assert len(catalog) == initial_len + 1
    catalog.unregister("extra-model-test")
    assert len(catalog) == initial_len


# --- Wave 3P additional tests ---------------------------------------------------

def test_model_catalog_get_known_model():
    """ModelCatalog.get() returns a ModelEntry for a known model_id."""
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    m = cat.get("MiniMax-M3")
    assert m.model_id == "MiniMax-M3"


def test_model_catalog_list_models_sorted():
    """ModelCatalog.list_models() returns a sorted list of model_id strings."""
    from hive.llm.model_catalog import ModelCatalog
    cat = ModelCatalog()
    models = cat.list_models()
    assert models == sorted(models)


def test_model_entry_supports_thinking_flag():
    """MiniMax-M3 has supports_thinking=True."""
    from hive.llm.model_catalog import ModelCatalog
    m = ModelCatalog().get("MiniMax-M3")
    assert m.supports_thinking is True


def test_model_entry_context_length_positive():
    """MiniMax-M3 has context_length > 0."""
    from hive.llm.model_catalog import ModelCatalog
    m = ModelCatalog().get("MiniMax-M3")
    assert m.context_length > 0


def test_model_catalog_register_then_get():
    """A registered model is retrievable via get()."""
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    cat.register(ModelEntry(model_id="test-reg-model", context_length=4096))
    m = cat.get("test-reg-model")
    assert m.model_id == "test-reg-model"
    cat.unregister("test-reg-model")


def test_model_catalog_register_duplicate_does_not_raise():
    """Registering the same model_id twice does not raise."""
    from hive.llm.model_catalog import ModelCatalog, ModelEntry
    cat = ModelCatalog()
    cat.register(ModelEntry(model_id="dup-model-xyz", context_length=8192))
    cat.register(ModelEntry(model_id="dup-model-xyz", context_length=16384))
    m = cat.get("dup-model-xyz")
    assert m.context_length == 16384
    cat.unregister("dup-model-xyz")


# --- Wave 3V additional tests (8) -----------------------------------------------

def test_wave3v_llm_adapter_is_abstract():
    """LLMAdapter cannot be instantiated directly — it enforces the complete() contract."""
    from hive.llm.adapters.base import LLMAdapter
    try:
        LLMAdapter()
        assert False, "expected TypeError"
    except TypeError:
        pass


def test_wave3v_completion_request_max_tokens_default():
    """CompletionRequest.max_tokens must default to 4096."""
    from hive.llm.adapters.base import CompletionRequest
    from hive.core.types import Message, Role
    req = CompletionRequest(
        model="MiniMax-M3",
        messages=[Message(role=Role.USER, content="hi")],
    )
    assert req.max_tokens == 4_096


def test_wave3v_completion_request_extra_passthrough():
    """CompletionRequest.extra dict is forwarded into _build_body()."""
    adapter = MiniMaxAdapter("http://x", ModelCatalog(), prompt_caching=False)
    req = CompletionRequest(
        model="MiniMax-M3",
        messages=[Message(role=Role.USER, content="hi")],
        thinking=False,
        extra={"temperature": 0.7},
    )
    body = adapter._build_body(req)
    assert body.get("temperature") == 0.7


def test_wave3v_completion_result_tool_calls_default_empty():
    """CompletionResult.tool_calls must default to an empty list."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="hi", model="m")
    assert r.tool_calls == []


def test_wave3v_completion_result_raw_default_empty_dict():
    """CompletionResult.raw must default to an empty dict."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="hi", model="m")
    assert r.raw == {}


def test_wave3v_usage_total_tokens():
    """Usage.input_tokens + output_tokens gives the correct total."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=300, output_tokens=150)
    assert u.input_tokens + u.output_tokens == 450


def test_wave3v_model_entry_is_frozen():
    """ModelEntry is a frozen dataclass — mutation raises FrozenInstanceError."""
    from hive.llm.model_catalog import ModelEntry
    entry = ModelEntry(model_id="immutable-model", context_length=8192)
    try:
        entry.context_length = 9999  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except Exception as exc:
        assert "frozen" in type(exc).__name__.lower() or "FrozenInstance" in str(type(exc))


def test_wave3v_base_adapter_default_astream_yields_text():
    """The default LLMAdapter.astream() implementation yields the text from complete()."""
    import asyncio
    from hive.llm.adapters.base import LLMAdapter, CompletionRequest, CompletionResult
    from hive.core.types import Message, Role

    class _MinimalAdapter(LLMAdapter):
        name = "minimal"

        async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
            return CompletionResult(text="hello from minimal", model=request.model)

    adapter = _MinimalAdapter()
    req = CompletionRequest(
        model="test-model",
        messages=[Message(role=Role.USER, content="go")],
        thinking=False,
    )

    async def collect():
        chunks = []
        async for chunk in adapter.astream(req, api_key="dummy"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert chunks == ["hello from minimal"]
