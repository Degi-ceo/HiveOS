"""M8 — provider flexibility: anthropic + codex adapters, provider registry, selection."""
from __future__ import annotations

import asyncio
import sys

import httpx
import pytest

from hive.core.config import HiveConfig
from hive.core.types import Message, Role
from hive.llm.adapters import make_adapter, PROVIDERS
from hive.llm.adapters.base import CompletionRequest

ECHO_STDIN = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
EXIT_FAILURE = [sys.executable, "-c", "raise SystemExit(1)"]
EXIT_SUCCESS = [sys.executable, "-c", "raise SystemExit(0)"]



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


def test_codex_adapter_complete_via_portable_echo():
    from hive.llm.adapters.codex import CodexAdapter
    adapter = CodexAdapter(cmd=ECHO_STDIN)  # echoes stdin
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
    planner = make_codex_planner(ECHO_STDIN, timeout=10)
    out = asyncio.run(planner([Message(role=Role.USER, content="hello")], None))
    assert "hello" in out
    with pytest.raises(PlannerError):
        asyncio.run(make_codex_planner(EXIT_FAILURE)([Message(role=Role.USER, content="x")], None))


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


# --- adapter registry extra ---------------------------------------------------

def test_providers_list_is_complete():
    from hive.llm.adapters import PROVIDERS
    assert "minimax" in PROVIDERS
    assert "anthropic" in PROVIDERS
    assert "codex" in PROVIDERS
    assert len(PROVIDERS) == 3


def test_make_adapter_returns_different_instances():
    from hive.llm.adapters import make_adapter
    a1 = make_adapter("minimax")
    a2 = make_adapter("minimax")
    assert a1 is not a2  # new instance each call


def test_anthropic_adapter_prompt_caching_default_true():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    a = AnthropicAdapter("https://api.anthropic.com")
    assert a._prompt_caching is True


def test_anthropic_adapter_custom_base_url():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    a = AnthropicAdapter("https://custom.api.example.com")
    assert "custom.api.example.com" in a._base


def test_codex_adapter_has_correct_name():
    from hive.llm.adapters.codex import CodexAdapter
    assert CodexAdapter().name == "codex"


def test_minimax_adapter_has_correct_name():
    from hive.llm.adapters.minimax import MiniMaxAdapter
    assert MiniMaxAdapter("http://x").name == "minimax"


# --- Additional M8 provider tests ---------------------------------------------------

def test_anthropic_adapter_is_subclass_of_minimax():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    from hive.llm.adapters.minimax import MiniMaxAdapter
    assert issubclass(AnthropicAdapter, MiniMaxAdapter)


def test_anthropic_adapter_default_base_url():
    from hive.llm.adapters.anthropic import AnthropicAdapter
    a = AnthropicAdapter()
    assert "anthropic.com" in a._base


def test_codex_adapter_complete_empty_output_raises():
    """CodexAdapter with 'true' command (exits 0, no output) raises PlannerError."""
    import pytest
    from hive.llm.adapters.codex import CodexAdapter, PlannerError
    from hive.llm.adapters.base import CompletionRequest

    from hive.core.types import Message, Role
    adapter = CodexAdapter(cmd=EXIT_SUCCESS)
    req = CompletionRequest(model="codex", messages=[Message(role=Role.USER, content="x")])
    with pytest.raises(PlannerError):
        asyncio.run(adapter.complete(req, api_key=""))


def test_minimax_adapter_name_is_string():
    from hive.llm.adapters.minimax import MiniMaxAdapter
    assert isinstance(MiniMaxAdapter("http://x").name, str)


def test_make_adapter_anthropic_has_anthropic_in_base():
    from hive.llm.adapters import make_adapter
    a = make_adapter("anthropic")
    assert "anthropic" in a._base.lower()


def test_make_adapter_minimax_has_minimax_in_base():
    from hive.llm.adapters import make_adapter
    a = make_adapter("minimax")
    assert "minimax" in a._base.lower() or "api" in a._base.lower()


def test_completion_result_has_text_and_model():
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="answer", model="fake-model")
    assert r.text == "answer" and r.model == "fake-model"


# --- Wave 3K additional tests -------------------------------------------------

def test_completion_request_system_is_optional():
    """CompletionRequest.system defaults to None."""
    from hive.llm.adapters.base import CompletionRequest

    req = CompletionRequest(model="m", messages=[])
    assert req.system is None


def test_completion_request_max_tokens_default():
    """CompletionRequest.max_tokens defaults to 4096."""
    from hive.llm.adapters.base import CompletionRequest

    req = CompletionRequest(model="m", messages=[])
    assert req.max_tokens == 4096


def test_usage_zero_defaults():
    """Usage initializes with zero tokens when not specified."""
    from hive.llm.adapters.base import Usage
    u = Usage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_usage_total_tokens():
    """Usage(input=3, output=7) has total of 10 tokens."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=3, output_tokens=7)
    assert u.input_tokens + u.output_tokens == 10


def test_completion_result_default_finish_reason():
    """CompletionResult.finish_reason defaults to 'stop'."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="hi", model="m")
    assert r.finish_reason == "stop"


def test_completion_result_empty_tool_calls_default():
    """CompletionResult.tool_calls defaults to an empty list."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="x", model="m")
    assert r.tool_calls == []


# ---------------------------------------------------------------------------
# Six additional tests (batch 4)
# ---------------------------------------------------------------------------

def test_render_prompt_no_system_has_no_context_block():
    """render_prompt without a system prompt must not include [CONTEXT]."""
    from hive.llm.adapters.codex import render_prompt
    out = render_prompt([Message(role=Role.USER, content="hi")], None)
    assert "[CONTEXT]" not in out
    assert "user: hi" in out


def test_codex_adapter_complete_always_returns_model_codex():
    """CodexAdapter.complete always tags the result model as 'codex', not the request model."""
    from hive.llm.adapters.codex import CodexAdapter
    adapter = CodexAdapter(cmd=ECHO_STDIN)
    req = CompletionRequest(model="some-other-model", messages=[Message(role=Role.USER, content="hi")])
    res = asyncio.run(adapter.complete(req, api_key=""))
    assert res.model == "codex"


def test_codex_adapter_complete_finish_reason_is_stop():
    """CodexAdapter.complete returns finish_reason='stop' on success."""
    from hive.llm.adapters.codex import CodexAdapter
    adapter = CodexAdapter(cmd=ECHO_STDIN)
    req = CompletionRequest(model="codex", messages=[Message(role=Role.USER, content="ping")])
    res = asyncio.run(adapter.complete(req, api_key=""))
    assert res.finish_reason == "stop"


def test_make_adapter_unknown_error_mentions_provider_name():
    """ValueError from make_adapter includes the unknown provider name in the message."""
    from hive.llm.adapters import make_adapter
    try:
        make_adapter("totally-unknown-xyz")
    except ValueError as exc:
        assert "totally-unknown-xyz" in str(exc)
    else:
        pytest.fail("Expected ValueError was not raised")


def test_anthropic_adapter_complete_returns_finish_reason():
    """AnthropicAdapter.complete maps stop_reason from the API response to finish_reason."""
    from hive.llm.adapters.anthropic import AnthropicAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "stop_reason": "max_tokens",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(client=client)
    req = CompletionRequest(model="claude-x", messages=[Message(role=Role.USER, content="go")])
    res = asyncio.run(adapter.complete(req, api_key="key"))
    assert res.finish_reason == "max_tokens"


def test_completion_request_thinking_defaults_to_true():
    """CompletionRequest.thinking defaults to True (thinking enabled by default)."""
    req = CompletionRequest(model="m", messages=[])
    assert req.thinking is True


# --- Wave 3Q additional tests -------------------------------------------------

def test_completion_result_raw_defaults_to_empty_dict():
    """CompletionResult.raw defaults to an empty dict, not None."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="x", model="m")
    assert r.raw == {} and isinstance(r.raw, dict)


def test_completion_request_tools_defaults_to_none():
    """CompletionRequest.tools defaults to None (no tool schemas)."""
    req = CompletionRequest(model="m", messages=[])
    assert req.tools is None


def test_completion_request_extra_defaults_to_empty_dict():
    """CompletionRequest.extra defaults to an empty dict."""
    req = CompletionRequest(model="m", messages=[])
    assert req.extra == {} and isinstance(req.extra, dict)


def test_llm_adapter_astream_default_yields_full_text():
    """LLMAdapter.astream default impl (no override) yields the complete text once."""
    from hive.llm.adapters.base import CompletionResult, CompletionRequest, LLMAdapter, Usage

    class _StubAdapter(LLMAdapter):
        name = "stub"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="hello world", model="stub")

    async def _collect():
        adapter = _StubAdapter()
        req = CompletionRequest(model="stub", messages=[Message(role=Role.USER, content="hi")])
        chunks = [chunk async for chunk in adapter.astream(req, api_key="")]
        return chunks

    chunks = asyncio.run(_collect())
    assert chunks == ["hello world"]


def test_anthropic_adapter_complete_maps_input_tokens():
    """AnthropicAdapter.complete populates usage.input_tokens from the API response."""
    from hive.llm.adapters.anthropic import AnthropicAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 11, "output_tokens": 4},
            "stop_reason": "end_turn",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(client=client)
    req = CompletionRequest(model="claude-x", messages=[Message(role=Role.USER, content="q")])
    res = asyncio.run(adapter.complete(req, api_key="key"))
    assert res.usage.input_tokens == 11


def test_usage_equality():
    """Two Usage instances with the same token counts compare equal."""
    from hive.llm.adapters.base import Usage
    assert Usage(input_tokens=5, output_tokens=10) == Usage(input_tokens=5, output_tokens=10)
    assert Usage(input_tokens=1, output_tokens=2) != Usage(input_tokens=1, output_tokens=3)


# --- Wave 3W-A tests -----------------------------------------------------------

def test_wave3w_completion_result_text_field():
    """CompletionResult.text stores the provided text value."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="some output", model="m")
    assert r.text == "some output"


def test_wave3w_completion_result_model_field():
    """CompletionResult.model stores the provided model identifier."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="x", model="claude-3-7")
    assert r.model == "claude-3-7"


def test_wave3w_usage_zero_input_tokens():
    """Usage with zero input_tokens remains zero."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=0, output_tokens=5)
    assert u.input_tokens == 0


def test_wave3w_usage_zero_output_tokens():
    """Usage with zero output_tokens remains zero."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=7, output_tokens=0)
    assert u.output_tokens == 0


def test_wave3w_completion_request_model_field():
    """CompletionRequest.model stores the value provided at construction."""
    req = CompletionRequest(model="test-model", messages=[])
    assert req.model == "test-model"


def test_wave3w_completion_request_messages_field():
    """CompletionRequest.messages stores the list passed at construction."""
    msgs = [Message(role=Role.USER, content="hello")]
    req = CompletionRequest(model="m", messages=msgs)
    assert req.messages is msgs


def test_wave3w_llm_adapter_subclass_name():
    """An LLMAdapter subclass can set a custom name attribute."""
    from hive.llm.adapters.base import LLMAdapter, CompletionRequest, CompletionResult

    class _MyAdapter(LLMAdapter):
        name = "my-provider"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="ok", model="my-provider")

    assert _MyAdapter().name == "my-provider"


def test_wave3w_llm_adapter_complete_returns_completion_result():
    """A minimal LLMAdapter subclass returns a CompletionResult from complete()."""
    from hive.llm.adapters.base import LLMAdapter, CompletionRequest, CompletionResult

    class _StubAdapter(LLMAdapter):
        name = "stub"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="stub response", model="stub")

    req = CompletionRequest(model="stub", messages=[Message(role=Role.USER, content="hi")])
    result = asyncio.run(_StubAdapter().complete(req, api_key=""))
    assert isinstance(result, CompletionResult)
    assert result.text == "stub response"


# --- Wave 4A additional tests -------------------------------------------------

def test_wave4a_llm_adapter_aclose_default_returns_none():
    """LLMAdapter.aclose() default no-op returns None (not NotImplemented)."""
    from hive.llm.adapters.base import LLMAdapter, CompletionResult

    class _MinimalAdapter(LLMAdapter):
        name = "minimal"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="x", model="minimal")

    result = asyncio.run(_MinimalAdapter().aclose())
    assert result is None


def test_wave4a_astream_empty_text_yields_nothing():
    """LLMAdapter.astream() default yields nothing when complete() returns empty text."""
    from hive.llm.adapters.base import LLMAdapter, CompletionResult

    class _EmptyAdapter(LLMAdapter):
        name = "empty"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="", model="empty")

    async def collect():
        return [c async for c in _EmptyAdapter().astream(
            CompletionRequest(model="empty", messages=[Message(role=Role.USER, content="hi")]),
            api_key="",
        )]

    assert asyncio.run(collect()) == []


def test_wave4a_completion_result_with_tool_calls_populated():
    """CompletionResult stores tool_calls when explicitly provided."""
    from hive.llm.adapters.base import CompletionResult
    from hive.core.types import ToolCall
    tc = ToolCall(id="tc1", name="search", arguments='{"q": "test"}')
    r = CompletionResult(text="", model="m", tool_calls=[tc])
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "search"


def test_wave4a_completion_request_all_fields_explicit():
    """CompletionRequest stores every field when all are set explicitly."""
    msgs = [Message(role=Role.USER, content="go")]
    tools = [{"name": "lookup", "description": "look up", "input_schema": {}}]
    req = CompletionRequest(
        model="x-model",
        messages=msgs,
        system="be helpful",
        max_tokens=512,
        thinking=False,
        tools=tools,
        extra={"temperature": 0.5},
    )
    assert req.model == "x-model"
    assert req.system == "be helpful"
    assert req.max_tokens == 512
    assert req.thinking is False
    assert req.tools is tools
    assert req.extra == {"temperature": 0.5}


def test_wave4a_usage_large_token_values():
    """Usage correctly stores large token counts without overflow."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=1_000_000, output_tokens=500_000)
    assert u.input_tokens == 1_000_000
    assert u.output_tokens == 500_000
    assert u.input_tokens + u.output_tokens == 1_500_000


def test_wave4a_anthropic_adapter_content_type_header():
    """AnthropicAdapter._headers() includes content-type: application/json."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    headers = adapter._headers("dummy-key")
    assert headers.get("content-type") == "application/json"


def test_wave4a_minimax_adapter_is_subclass_of_llm_adapter():
    """MiniMaxAdapter is a subclass of LLMAdapter."""
    from hive.llm.adapters.minimax import MiniMaxAdapter
    from hive.llm.adapters.base import LLMAdapter
    assert issubclass(MiniMaxAdapter, LLMAdapter)


def test_wave4a_completion_result_raw_stores_passed_dict():
    """CompletionResult.raw stores the dict that was explicitly passed in."""
    from hive.llm.adapters.base import CompletionResult
    data = {"stop_reason": "end_turn", "id": "msg-abc"}
    r = CompletionResult(text="hi", model="m", raw=data)
    assert r.raw is data
    assert r.raw["stop_reason"] == "end_turn"


# --- Wave 4G-A additional tests -----------------------------------------------

def test_wave4g_usage_input_tokens_type():
    """Usage.input_tokens is always an int."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=42, output_tokens=0)
    assert isinstance(u.input_tokens, int)


def test_wave4g_usage_output_tokens_type():
    """Usage.output_tokens is always an int."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=0, output_tokens=7)
    assert isinstance(u.output_tokens, int)


def test_wave4g_anthropic_adapter_version_header():
    """AnthropicAdapter._headers() includes anthropic-version header."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    h = AnthropicAdapter()._headers("k")
    assert "anthropic-version" in h and h["anthropic-version"]


def test_wave4g_anthropic_adapter_api_key_in_header():
    """AnthropicAdapter._headers() passes the api_key as x-api-key."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    h = AnthropicAdapter()._headers("secret-key-abc")
    assert h["x-api-key"] == "secret-key-abc"


def test_wave4g_completion_request_extra_passed_through_body():
    """Extra dict fields (e.g. temperature) appear in the request body built by MiniMaxAdapter."""
    import json
    import httpx
    from hive.llm.adapters.minimax import MiniMaxAdapter
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("https://api.minimax.io", client=client)
    req = CompletionRequest(
        model="minimax-m1",
        messages=[Message(role=Role.USER, content="hi")],
        extra={"temperature": 0.3},
    )
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured["body"].get("temperature") == 0.3


def test_wave4g_completion_request_max_tokens_in_body():
    """max_tokens from CompletionRequest ends up in the JSON body sent to the provider."""
    import json
    import httpx
    from hive.llm.adapters.minimax import MiniMaxAdapter
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "y"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MiniMaxAdapter("https://api.minimax.io", client=client)
    req = CompletionRequest(
        model="minimax-m1",
        messages=[Message(role=Role.USER, content="q")],
        max_tokens=256,
    )
    asyncio.run(adapter.complete(req, api_key="k"))
    assert captured["body"]["max_tokens"] == 256


def test_wave4g_completion_result_usage_defaults_zero():
    """CompletionResult built without explicit usage has Usage(0, 0)."""
    from hive.llm.adapters.base import CompletionResult, Usage
    r = CompletionResult(text="hi", model="m")
    assert r.usage == Usage(input_tokens=0, output_tokens=0)


def test_wave4g_anthropic_adapter_complete_text():
    """AnthropicAdapter.complete returns the text block from API response."""
    import httpx
    from hive.llm.adapters.anthropic import AnthropicAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "deep answer"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(client=client)
    req = CompletionRequest(model="claude-3-7", messages=[Message(role=Role.USER, content="q")])
    res = asyncio.run(adapter.complete(req, api_key="k"))
    assert res.text == "deep answer"


# --- Wave 4R additional tests -------------------------------------------------------

def test_wave4r_anthropic_adapter_name_property_returns_anthropic():
    """AnthropicAdapter.name returns 'anthropic' on both the class and instance."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    assert AnthropicAdapter.name == "anthropic"
    assert AnthropicAdapter().name == "anthropic"


def test_wave4r_anthropic_adapter_no_api_key_still_constructs():
    """AnthropicAdapter requires no api_key at construction — it accepts one at call time."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter()
    assert adapter is not None
    assert adapter.name == "anthropic"


def test_wave4r_anthropic_adapter_headers_include_expected_keys():
    """AnthropicAdapter._headers() includes x-api-key, anthropic-version, content-type."""
    from hive.llm.adapters.anthropic import AnthropicAdapter
    h = AnthropicAdapter()._headers("test-key-xyz")
    assert "x-api-key" in h
    assert "anthropic-version" in h
    assert "content-type" in h


def test_wave4r_completion_request_tools_list_stored():
    """CompletionRequest stores a tools list exactly as passed."""
    tools = [
        {"name": "get_weather", "description": "get weather", "input_schema": {"type": "object"}},
        {"name": "search", "description": "search the web", "input_schema": {"type": "object"}},
    ]
    req = CompletionRequest(model="m", messages=[], tools=tools)
    assert req.tools is tools
    assert len(req.tools) == 2
    assert req.tools[0]["name"] == "get_weather"


def test_wave4r_completion_request_messages_stored():
    """CompletionRequest.messages stores exactly the list passed at construction."""
    msgs = [
        Message(role=Role.USER, content="first"),
        Message(role=Role.ASSISTANT, content="second"),
    ]
    req = CompletionRequest(model="m", messages=msgs)
    assert req.messages is msgs
    assert len(req.messages) == 2


def test_wave4r_usage_arithmetic_input_plus_output_equals_total():
    """Usage input_tokens + output_tokens gives the expected sum."""
    from hive.llm.adapters.base import Usage
    u = Usage(input_tokens=123, output_tokens=456)
    assert u.input_tokens + u.output_tokens == 579


def test_wave4r_anthropic_adapter_complete_raises_on_http_error():
    """AnthropicAdapter.complete raises an httpx error when the server returns 4xx."""
    from hive.llm.adapters.anthropic import AnthropicAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "bad key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(client=client)
    req = CompletionRequest(model="claude-x", messages=[Message(role=Role.USER, content="hi")])
    try:
        asyncio.run(adapter.complete(req, api_key="bad-key"))
        assert False, "expected an HTTP error to be raised"
    except httpx.HTTPStatusError:
        pass


def test_wave4r_completion_result_finish_reason_stored():
    """CompletionResult stores the finish_reason string we pass at construction."""
    from hive.llm.adapters.base import CompletionResult
    r = CompletionResult(text="ok", model="m", finish_reason="max_tokens")
    assert r.finish_reason == "max_tokens"
