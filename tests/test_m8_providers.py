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
    adapter = CodexAdapter(cmd="true")
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
    adapter = CodexAdapter(cmd="cat")
    req = CompletionRequest(model="some-other-model", messages=[Message(role=Role.USER, content="hi")])
    res = asyncio.run(adapter.complete(req, api_key=""))
    assert res.model == "codex"


def test_codex_adapter_complete_finish_reason_is_stop():
    """CodexAdapter.complete returns finish_reason='stop' on success."""
    from hive.llm.adapters.codex import CodexAdapter
    adapter = CodexAdapter(cmd="cat")
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
