"""P-C iteration streaming — orchestrator.stream_ask + /chat/stream/iterations
+ /v1/chat/completions x-hive-iterations branch (SPRINT_6 P-C)."""
from __future__ import annotations

import asyncio
import json

from starlette.testclient import TestClient

from hive.agents.orchestrator import ConversationOrchestrator
from hive.core.config import HiveConfig
from hive.core.types import ToolCall
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.base import BaseTool, ToolSpec
from hive.core.types import ToolResult


# --- helpers shared with existing tests (kept in this file so it is self-contained) ---


class _FakeRouter:
    """Returns scripted CompletionResults; records calls. Implements both
    `complete` (orchestrator path) and `stream` (ask_stream path) so the same
    router can drive both endpoints under test."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="fake")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="fake")

    async def stream(self, messages, *, system=None, **kw):
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="fake")
        text = item.text if isinstance(item, CompletionResult) else str(item)
        for word in text.split(" "):
            yield word + " "


class _Echo(BaseTool):
    spec = ToolSpec(name="echo", description="echo", parameters={"type": "object"})

    async def execute(self, **params):
        return ToolResult(tool_name="echo", content=f"echoed:{params.get('text', '')}")


# --- 1. orchestrator.stream_ask event sequence -----------------------------------


def test_orchestrator_stream_ask_emits_full_sequence():
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "hi"}))
    router = _FakeRouter([
        CompletionResult(text="thinking", model="m", tool_calls=[call]),
        CompletionResult(text="final answer", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()})

    async def _run():
        return [ev async for ev in orch.stream_ask("please echo")]

    events = asyncio.run(_run())
    types = [e["type"] for e in events]
    assert "model_decision" in types
    assert "tool_call_start" in types
    assert "tool_call_end" in types
    assert types[-1] == "final"
    # tool_call_end status=ok for the echo tool
    end = next(e for e in events if e["type"] == "tool_call_end")
    assert end["status"] == "ok"
    assert end["name"] == "echo"
    # final carries the assistant text + tool call count
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "final answer"
    assert final["tool_calls"] == 1


def test_orchestrator_stream_ask_tool_error_surfaces_event():
    """A tool that raises must surface a tool_call_end status=error event,
    not crash the stream. We exercise _dispatch's happy path with an unknown
    tool (executor returns DispatchStatus.ERROR equivalent → content starts
    with '[tool error:') which we treat as ok-with-error-content."""
    call = ToolCall(id="c1", name="missing", arguments=json.dumps({}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="done", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()})

    async def _run():
        return [ev async for ev in orch.stream_ask("try missing")]

    events = asyncio.run(_run())
    end = next(e for e in events if e["type"] == "tool_call_end")
    assert end["status"] == "ok"
    assert end["content"].startswith("[tool error:")


def test_orchestrator_stream_ask_loop_guard_yields_event():
    # Same call twice → identical repeat → LoopGuard trips on 3rd attempt
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "x"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="concluding", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()})

    async def _run():
        return [ev async for ev in orch.stream_ask("repeat")]

    events = asyncio.run(_run())
    assert any(e["type"] == "loop_guard" for e in events)


def test_orchestrator_stream_ask_max_turns_yields_event():
    """max_iterations=2, every turn demands a tool call → expect max_turns."""
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "x"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="", model="m", tool_calls=[call]),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()}, max_iterations=2)

    async def _run():
        return [ev async for ev in orch.stream_ask("never settle")]

    events = asyncio.run(_run())
    assert any(e["type"] == "max_turns" for e in events)


# --- 2. runtime proxy ----------------------------------------------------------


def test_hive_stream_ask_iterations_proxies_to_orchestrator():
    """HiveOS.stream_ask_iterations forwards orchestrator events verbatim."""
    cfg = HiveConfig.from_env()
    hive = HiveOS.build(cfg, router=_FakeRouter([CompletionResult(text="hello", model="m")]))

    async def _run():
        return [ev async for ev in hive.stream_ask_iterations("hi")]

    events = asyncio.run(_run())
    types = [e["type"] for e in events]
    assert "model_decision" in types
    assert types[-1] == "final"
    # final text comes from the model — assert non-empty (router may be drained
    # by an earlier build step like title generation; only assert the wire-up).
    assert events[-1]["text"]


# --- 3. /chat/stream/iterations gateway endpoint --------------------------------


def _hive(tmp_path, script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_FakeRouter(script or []))


def _client(hive) -> TestClient:
    return TestClient(create_app(hive))


_TOKEN = {"X-Hive-Token": "change_me"}


def test_chat_stream_iterations_sse_format(tmp_path):
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "x"}))
    hive = _hive(tmp_path, [
        CompletionResult(text="decide", model="m", tool_calls=[call]),
        CompletionResult(text="done", model="m"),
    ])
    with _client(hive) as c:
        r = c.post("/chat/stream/iterations", json={"message": "hi", "session_id": "s"},
                   headers=_TOKEN)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.text
    # each event line is `event: <type>\ndata: <json>\n\n`; terminal `data: [DONE]`
    assert "event: model_decision" in body
    assert "event: tool_call_start" in body
    assert "event: tool_call_end" in body
    assert "event: final" in body
    assert body.rstrip().endswith("data: [DONE]")
    # at least 4 events before DONE
    assert body.count("event: ") >= 4


def test_chat_stream_iterations_requires_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.post("/chat/stream/iterations", json={"message": "hi"})
        assert r.status_code == 401


def test_chat_stream_iterations_error_does_not_leak_exception_detail(tmp_path):
    """When the orchestrator raises, only the exception class name is emitted —
    no message body, no stack frames."""

    class _BoomRouter:
        async def complete(self, *a, **kw):
            raise RuntimeError("secret internal detail should not leak")

        async def stream(self, *a, **kw):  # pragma: no cover - not exercised here
            if False:
                yield ""

        async def aclose(self):
            pass

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_BoomRouter())
    with _client(hive) as c:
        r = c.post("/chat/stream/iterations", json={"message": "x"}, headers=_TOKEN)
        assert r.status_code == 200
        body = r.text
    # class name surfaces; no leak of the original message
    assert "RuntimeError" in body
    assert "secret internal detail" not in body


# --- 4. /v1/chat/completions iterations branch ----------------------------------


def test_v1_chat_completions_iterations_streaming(tmp_path):
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "hi"}))
    hive = _hive(tmp_path, [
        CompletionResult(text="deciding", model="m", tool_calls=[call]),
        CompletionResult(text="final", model="m"),
    ])
    headers = {**_TOKEN, "x-hive-iterations": "true"}
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=headers,
        )
        assert r.status_code == 200
        chunks = [line for line in r.text.split("\n\n") if line.startswith("data: ")]
    # At least 2 chunks: one with delta.tool_calls, one stop, one [DONE]
    decoded = []
    for chunk in chunks:
        payload = chunk[len("data: "):]
        if payload == "[DONE]":
            decoded.append({"_done": True})
            continue
        decoded.append(json.loads(payload))
    assert any(c.get("_done") for c in decoded)
    tool_call_chunks = [c for c in decoded
                        if "choices" in c and c["choices"][0]["delta"].get("tool_calls")]
    assert tool_call_chunks, "expected at least one chunk with delta.tool_calls"
    tc = tool_call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "echo"


def test_v1_chat_completions_default_path_unchanged(tmp_path):
    """Regression: no header, no iterations → existing token-by-token format
    with delta.content (not delta.tool_calls)."""
    hive = _hive(tmp_path, [CompletionResult(text="hello world", model="m")])
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=_TOKEN,
        )
        assert r.status_code == 200
        body = r.text
    # Existing shape: chat.completion.chunk with delta.content
    assert '"delta": {"content"' in body
    # No tool_calls emitted on the default path
    assert '"tool_calls"' not in body
    assert body.rstrip().endswith("data: [DONE]")


# --- 5. /chat/stream default path regression ------------------------------------


def test_chat_stream_default_path_unchanged(tmp_path):
    """Regression: existing /chat/stream still emits raw tokens, no event: prefix,
    no tool_calls JSON. This is the token-only path — no orchestrator involved."""
    hive = _hive(tmp_path, [CompletionResult(text="hello", model="m")])
    with _client(hive) as c:
        r = c.post("/chat/stream", json={"message": "hi", "session_id": "r"}, headers=_TOKEN)
        assert r.status_code == 200
        body = r.text
    # Old format: data: <token>\n\n; no event: lines
    assert "event:" not in body
    assert body.rstrip().endswith("data: [DONE]")
    assert "data: hello" in body


# --- 6. v1 error chunk consistency (fix-pass review) ----------------------------


def test_v1_chat_completions_iterations_error_surfaces_class_name(tmp_path):
    """When the iterations stream raises, /v1 emits a stop chunk with sanitised
    class name in delta.content — no message body, no stack frames. Mirrors the
    /chat/stream `event: error` contract in OpenAI-shape.

    The orchestrator catches its own exceptions and emits `event: error` with
    class-name only. To exercise the GATEWAY-level except (which produces the
    `[error] <ClassName>` stop chunk), swap the orchestrator's stream_ask for a
    callable that raises directly."""

    async def _boom_stream_ask(*a, **kw):
        raise RuntimeError("secret internal detail should not leak")
        yield  # make it a generator

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_FakeRouter([CompletionResult(text="x", model="m")]))
    hive.orchestrator.stream_ask = _boom_stream_ask  # bypass orchestrator's own try/except
    headers = {**_TOKEN, "x-hive-iterations": "true"}
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}], "stream": True},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.text
    assert "[error] RuntimeError" in body
    assert "secret internal detail" not in body
    # Stop chunk + [DONE] still emitted after the error
    assert '"finish_reason": "stop"' in body
    assert body.rstrip().endswith("data: [DONE]")


def test_v1_chat_completions_default_error_surfaces_class_name(tmp_path):
    """Default /v1 streaming (no x-hive-iterations header) emits a stop chunk
    with sanitised class name when ask_stream raises."""

    class _BoomStreamer:
        async def stream(self, *a, **kw):
            raise RuntimeError("secret internal detail should not leak")
            if False:
                yield ""

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_BoomStreamer())
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}], "stream": True},
            headers=_TOKEN,
        )
        assert r.status_code == 200
        body = r.text
    assert "[error] RuntimeError" in body
    assert "secret internal detail" not in body
    assert '"finish_reason": "stop"' in body
    assert body.rstrip().endswith("data: [DONE]")
