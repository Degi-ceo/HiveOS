"""test_a2a.py — SPRINT_6 P-D A2A envelope coverage (issue #72).

100% coverage on src/hive/agents/a2a/* + a snapshot test that the existing
delegate_to_specialist callers see no behavior change after envelope refactor.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from hive.agents.a2a import (
    A2AClient,
    A2AConnectionError,
    A2AError,
    A2ARequest,
    A2AResponse,
)
from hive.agents.a2a import (
    register as a2a_register,
)
from hive.agents.a2a import (
    register_remote,
    route as a2a_route,
    registered_methods,
    unregister,
)
from hive.agents.a2a.router import A2ARoutingError
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS

_TOKEN = {"X-Hive-Token": "change_me"}


class _ScriptRouter:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, *, system="", tools=None, **kw):
        self.calls += 1
        return CompletionResult(text="ok", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


@pytest.fixture(autouse=True)
def _a2a_reset_between_tests():
    for m in list(registered_methods()):
        unregister(m)
    yield
    for m in list(registered_methods()):
        unregister(m)


# ---------------------------------------------------------------------------
# Envelope models
# ---------------------------------------------------------------------------

def test_envelope_request_default_id_is_uuid_hex():
    req = A2ARequest(method="researcher.run")
    assert isinstance(req.id, str) and len(req.id) == 32
    assert req.method == "researcher.run"
    assert req.params == {}


def test_envelope_request_extra_forbidden():
    with pytest.raises(Exception):
        A2ARequest.model_validate({"method": "x", "extra_field": "nope"})


def test_envelope_response_default_no_error():
    resp = A2AResponse(id="abc")
    assert resp.result is None
    assert resp.error is None
    assert resp.is_error() is False


def test_envelope_response_with_error():
    err = A2AError(code=-32601, message="not found", data={"method": "x"})
    resp = A2AResponse(id="abc", error=err)
    assert resp.is_error() is True
    assert resp.error.code == -32601
    assert resp.error.message == "not found"
    assert resp.error.data == {"method": "x"}


def test_envelope_roundtrip_dump_validate():
    req = A2ARequest(id="abc", method="x", params={"task": "hi"})
    raw = req.model_dump_json()
    parsed = A2ARequest.model_validate_json(raw)
    assert parsed == req
    err = A2AError(code=-32603, message="boom")
    resp = A2AResponse(id="abc", error=err)
    dumped = json.loads(resp.model_dump_json(exclude_none=True))
    assert dumped == {"id": "abc", "error": {"code": -32603, "message": "boom"}}


# ---------------------------------------------------------------------------
# Router — local + remote + errors
# ---------------------------------------------------------------------------

def test_router_registered_methods_empty_initially():
    assert registered_methods() == []


def test_router_local_handler_routes_to_result():
    async def handler(params):
        return {"reply": "hi " + params["name"]}

    a2a_register("greet.run", handler)
    assert "greet.run" in registered_methods()

    async def go():
        return await a2a_route("req1", "greet.run", {"name": "world"})

    resp = asyncio.run(go())
    assert resp.id == "req1"
    assert resp.result == {"reply": "hi world"}
    assert resp.error is None


def test_router_unknown_method_returns_method_not_found():
    async def go():
        return await a2a_route("req2", "no.such.method", {})

    resp = asyncio.run(go())
    assert resp.error is not None
    assert resp.error.code == -32601
    assert "no.such.method" in resp.error.message


def test_router_handler_exception_normalised_to_internal_error():
    async def handler(params):
        raise RuntimeError("kaboom")

    a2a_register("boom.run", handler)

    async def go():
        return await a2a_route("req3", "boom.run", {})

    resp = asyncio.run(go())
    assert resp.error is not None
    assert resp.error.code == -32603
    assert "kaboom" in resp.error.message
    assert resp.error.data == {"type": "RuntimeError"}


def test_router_register_remote_requires_http():
    with pytest.raises(A2ARoutingError):
        register_remote("r.x", "ftp://bad")


def test_router_register_remote_returns_uri():
    register_remote("r.x", "https://example.com/rpc")
    assert "r.x" in registered_methods()

    async def go():
        return await a2a_route("req4", "r.x", {"task": "x"})

    resp = asyncio.run(go())
    assert resp.error is None
    assert resp.result == {"remote_uri": "https://example.com/rpc"}


def test_router_unregister_removes_method():
    async def handler(params):
        return 1

    a2a_register("tmp.run", handler)
    assert "tmp.run" in registered_methods()
    unregister("tmp.run")
    assert "tmp.run" not in registered_methods()
    unregister("tmp.run")  # idempotent


# ---------------------------------------------------------------------------
# Client — endpoint validation, success, retry, failure
# ---------------------------------------------------------------------------

def test_client_rejects_non_http_endpoint():
    with pytest.raises(A2AConnectionError):
        A2AClient("ftp://bad")


def test_client_call_success():
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"id": "abc", "result": {"ok": True}},
    ))
    async def go():
        async with A2AClient("http://test/rpc", client=httpx.AsyncClient(
            transport=transport, timeout=5.0,
        )) as c:
            return await c.call("echo", {"x": 1}, request_id="abc")

    resp = asyncio.run(go())
    assert resp.id == "abc"
    assert resp.result == {"ok": True}
    assert resp.error is None


def test_client_call_4xx_returns_envelope():
    transport = httpx.MockTransport(lambda req: httpx.Response(
        400, json={"id": "abc", "error": {"code": -32601, "message": "bad"}},
    ))
    async def go():
        async with A2AClient("http://test/rpc", client=httpx.AsyncClient(
            transport=transport, timeout=5.0,
        )) as c:
            return await c.call("bad", request_id="abc")

    resp = asyncio.run(go())
    assert resp.error is not None
    assert resp.error.code == -32601


def test_client_call_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async def go():
        async with A2AClient("http://test/rpc", max_retries=2, backoff=0.0,
                              client=httpx.AsyncClient(transport=transport, timeout=5.0)) as c:
            return await c.call("x")

    with pytest.raises(A2AConnectionError):
        asyncio.run(go())
    assert calls["n"] == 3  # initial + 2 retries


def test_client_call_5xx_then_200_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="transient")
        return httpx.Response(200, json={"id": "r", "result": "ok"})

    transport = httpx.MockTransport(handler)
    async def go():
        async with A2AClient("http://test/rpc", max_retries=2, backoff=0.0,
                              client=httpx.AsyncClient(transport=transport, timeout=5.0)) as c:
            return await c.call("x", request_id="r")

    resp = asyncio.run(go())
    assert resp.result == "ok"
    assert calls["n"] == 2


def test_client_connection_error_retries_then_raises():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    async def go():
        async with A2AClient("http://test/rpc", max_retries=1, backoff=0.0,
                              client=httpx.AsyncClient(transport=transport, timeout=5.0)) as c:
            return await c.call("x")

    with pytest.raises(A2AConnectionError):
        asyncio.run(go())
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# End-to-end: delegate_to_specialist wired through the envelope
# ---------------------------------------------------------------------------

def test_delegate_via_envelope_routes_through_router():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate_via_envelope, register_agent
    from hive.agents.executor import AgentExecutor

    class _Stub(BaseAgent):
        agent_id = "stub"
        async def run(self, input, context=None, **kw):
            return AgentResult(content=f"handled:{input}")

    register_agent("test-stub", lambda: _Stub())

    async def go():
        return await delegate_via_envelope("hi", "test-stub", executor=AgentExecutor())

    res = asyncio.run(go())
    assert res.content == "handled:hi"


def test_delegate_via_envelope_handles_failure():
    from hive.agents.base import BaseAgent
    from hive.agents.delegate import delegate_via_envelope, register_agent
    from hive.agents.executor import AgentExecutor

    class _Fail(BaseAgent):
        agent_id = "fail"
        async def run(self, task, context=None, **kw):
            raise RuntimeError("nope")

    register_agent("test-fail", lambda: _Fail())

    async def go():
        return await delegate_via_envelope("hi", "test-fail", executor=AgentExecutor())

    res = asyncio.run(go())
    assert "subagent failed" in res.content


def test_delegate_via_envelope_unknown_agent_returns_error():
    """Unknown agent → KeyError in handler → envelope error → AgentResult with error."""
    from hive.agents.delegate import delegate_via_envelope

    async def go():
        return await delegate_via_envelope("hi", "ghost-agent-zz")

    res = asyncio.run(go())
    assert "no agent registered" in res.content


# ---------------------------------------------------------------------------
# Snapshot test: existing delegate_to_specialist callers see no behavior change
# ---------------------------------------------------------------------------

def test_snapshot_delegate_to_specialist_output_unchanged(tmp_path, monkeypatch):
    """Pre-refactor output ("specialist reply") must equal post-refactor output.

    Two layers of evidence:
    (a) the tool wrapper contract (monkeypatched delegate_via_envelope) — proves
        the wrapper shape is unchanged,
    (b) a real agent invoked end-to-end through the envelope pipeline — proves
        the envelope path itself produces the expected AgentResult content.
    """
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import register_agent

    # (a) Wrapper-shape snapshot
    h = _hive(tmp_path)

    async def fake_via_envelope(task, name, *, executor=None):
        return AgentResult(content="specialist reply")

    monkeypatch.setattr("hive.agents.delegate.delegate_via_envelope", fake_via_envelope)
    res = asyncio.run(h.tools["delegate_to_specialist"].execute(
        agent="researcher", task="find x"))
    assert res.content == "specialist reply"

    async def fake_via_envelope_raises(task, name, *, executor=None):
        raise KeyError(name)

    monkeypatch.setattr("hive.agents.delegate.delegate_via_envelope",
                        fake_via_envelope_raises)
    res = asyncio.run(h.tools["delegate_to_specialist"].execute(
        agent="ghost", task="do x"))
    assert res.content.startswith("[delegate error:")

    # (b) End-to-end golden snapshot — real agent through real envelope pipeline.
    #     If delegate_via_envelope returns garbage, this assertion fails.
    monkeypatch.undo()

    class _GoldenStub(BaseAgent):
        agent_id = "golden"
        async def run(self, input, context=None, **kw):
            return AgentResult(content=f"golden:{input}")

    register_agent("snapshot-stub", lambda: _GoldenStub())

    res = asyncio.run(h.tools["delegate_to_specialist"].execute(
        agent="snapshot-stub", task="ping"))
    assert res.content == "golden:ping"


# ---------------------------------------------------------------------------
# Gateway endpoint
# ---------------------------------------------------------------------------

def test_a2a_rpc_requires_auth(tmp_path):
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.post("/a2a/rpc", json={"method": "echo", "params": {}})
        assert r.status_code == 401


def test_a2a_rpc_local_dispatch(tmp_path):
    async def handler(params):
        return {"reply": "ok"}

    a2a_register("echo.run", handler)
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.post("/a2a/rpc", json={
            "id": "req1", "method": "echo.run", "params": {},
        }, headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "req1"
        assert body["result"] == {"reply": "ok"}
        assert "error" not in body


def test_a2a_rpc_unknown_method_returns_envelope_error(tmp_path):
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.post("/a2a/rpc", json={
            "id": "req2", "method": "missing.run", "params": {},
        }, headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == -32601


def test_a2a_rpc_invalid_envelope_returns_422(tmp_path):
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.post("/a2a/rpc", json={"extra_field": True}, headers=_TOKEN)
        assert r.status_code == 422


def test_a2a_rpc_handler_exception_returns_envelope_error(tmp_path):
    async def handler(params):
        raise ValueError("boom")

    a2a_register("fail.run", handler)
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.post("/a2a/rpc", json={
            "id": "req3", "method": "fail.run", "params": {},
        }, headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == -32603
        assert "boom" in body["error"]["message"]


# ---------------------------------------------------------------------------
# SPRINT_6 P-G (issue #75): delegate_via_envelope emits A2A events
# ---------------------------------------------------------------------------

def test_delegate_via_envelope_emits_started_completed_events(tmp_path):
    """delegate_via_envelope must publish a2a.call.started + completed on success."""
    from hive.agents.a2a import emit_call_started  # re-export check
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate_via_envelope, register_agent
    from hive.agents.executor import AgentExecutor
    from hive.core.events import EventBus, EventType

    class _Stub(BaseAgent):
        agent_id = "ev-stub"
        async def run(self, input, context=None, **kw):
            return AgentResult(content="ok")

    register_agent("ev-stub", lambda: _Stub())
    bus = EventBus()
    started, completed = [], []
    bus.subscribe(EventType.A2A_CALL_STARTED, started.append)
    bus.subscribe(EventType.A2A_CALL_COMPLETED, completed.append)

    async def go():
        return await delegate_via_envelope(
            "hi", "ev-stub", executor=AgentExecutor(),
            bus=bus, session_id="sess-1",
        )

    res = asyncio.run(go())
    assert res.content == "ok"
    assert len(started) == 1
    assert started[0].data["agent_name"] == "ev-stub"
    assert started[0].data["task"] == "hi"
    assert started[0].data["session_id"] == "sess-1"
    assert len(completed) == 1
    assert completed[0].data["request_id"] == started[0].data["request_id"]


def test_delegate_via_envelope_emits_failed_event_on_error(tmp_path):
    """delegate_via_envelope must publish a2a.call.failed when the handler raises."""
    from hive.agents.base import BaseAgent
    from hive.agents.delegate import delegate_via_envelope, register_agent
    from hive.agents.executor import AgentExecutor
    from hive.core.events import EventBus, EventType

    class _Boom(BaseAgent):
        agent_id = "boom"
        async def run(self, task, context=None, **kw):
            raise RuntimeError("kaboom")

    register_agent("boom", lambda: _Boom())
    bus = EventBus()
    failed = []
    bus.subscribe(EventType.A2A_CALL_FAILED, failed.append)

    async def go():
        return await delegate_via_envelope("hi", "boom", executor=AgentExecutor(), bus=bus)

    res = asyncio.run(go())
    assert "subagent failed" in res.content
    assert len(failed) == 1
    assert failed[0].data["error"]  # non-empty
