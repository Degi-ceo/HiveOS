"""M4 surfaces — SSE streaming + transport-only Telegram channel."""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.core.types import Message, Role
from hive.gateway.app import create_app
from hive.gateway.channels.base import MessageEvent, OutgoingMessage, SendResult
from hive.gateway.channels.telegram import TelegramChannel
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
from hive.llm.credential_pool import CredentialPool
from hive.llm.router import ModelRouter
from hive.runtime import HiveOS


# --- adapter astream default ---------------------------------------------------

class _OneShotAdapter(LLMAdapter):
    name = "oneshot"

    async def complete(self, request, *, api_key):
        return CompletionResult(text="hello world", model=request.model,
                                usage=Usage(output_tokens=2))


def test_default_astream_yields_full_text():
    adapter = _OneShotAdapter()
    req = CompletionRequest(model="m", messages=[Message(role=Role.USER, content="hi")])

    async def collect():
        return [d async for d in adapter.astream(req, api_key="k")]

    assert asyncio.run(collect()) == ["hello world"]


# --- router.stream -------------------------------------------------------------

class _DeltaAdapter(LLMAdapter):
    name = "delta"

    async def complete(self, request, *, api_key):
        return CompletionResult(text="unused", model=request.model)

    async def astream(self, request, *, api_key):
        for part in ("Hel", "lo ", "Hive"):
            yield part


def _config(tmp_path):
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_router_stream_yields_deltas_and_emits(tmp_path):
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.INFERENCE_END, lambda e: seen.append(e))
    router = ModelRouter(config=_config(tmp_path), adapter=_DeltaAdapter(),
                         credential_pool=CredentialPool(["k"]), events=bus)

    async def collect():
        return [d async for d in router.stream([Message(role=Role.USER, content="hi")])]

    assert asyncio.run(collect()) == ["Hel", "lo ", "Hive"]
    assert seen, "INFERENCE_END should fire after a stream"


def test_router_stream_budget_blocks(tmp_path):
    from hive.llm.router import BudgetError
    router = ModelRouter(config=_config(tmp_path), adapter=_DeltaAdapter(),
                         credential_pool=CredentialPool(["k"]),
                         budget=lambda: (False, "cap hit"))

    async def collect():
        return [d async for d in router.stream([Message(role=Role.USER, content="hi")])]

    with pytest.raises(BudgetError):
        asyncio.run(collect())


# --- gateway SSE ---------------------------------------------------------------

class _StreamRouter:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="full", model="m")

    async def stream(self, messages, *, system=None, **kw):
        for part in ("a", "b", "c"):
            yield part

    async def aclose(self):
        pass


def test_chat_stream_sse(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())
    with TestClient(create_app(hive)) as c:
        r = c.post("/chat/stream", json={"message": "hi", "session_id": "s1"},
                   headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 200
        body = r.text
        assert "data: a" in body and "data: b" in body and "data: c" in body
        assert "data: [DONE]" in body


def test_chat_stream_requires_token(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())
    with TestClient(create_app(hive)) as c:
        assert c.post("/chat/stream", json={"message": "hi"}).status_code == 401


# --- Telegram channel ----------------------------------------------------------

def test_parse_update_text_message():
    ch = TelegramChannel("tok")
    evt = ch.parse_update({"message": {"message_id": 5, "text": "hello",
                                       "chat": {"id": 99}, "from": {"id": 7}}})
    assert isinstance(evt, MessageEvent)
    assert evt.text == "hello" and evt.chat_id == "99" and evt.user_id == "7"
    assert evt.message_id == "5" and evt.platform == "telegram"


def test_parse_update_ignores_nontext():
    ch = TelegramChannel("tok")
    assert ch.parse_update({"edited_message": {"text": "x"}}) is None
    assert ch.parse_update({"message": {"chat": {"id": 1}}}) is None  # no text


def test_telegram_send_via_fake_transport():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = TelegramChannel("tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="99", text="hi")))
    assert res.ok and res.message_id == "42"


def test_telegram_send_error_surfaces():
    import httpx

    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = TelegramChannel("tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="0", text="hi")))
    assert not res.ok and "chat not found" in res.error


# --- gateway Telegram webhook (injected fake channel) --------------------------

class _FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    def parse_update(self, update):
        msg = update.get("message")
        if not msg or "text" not in msg:
            return None
        return MessageEvent(text=msg["text"], chat_id="42", message_id="1",
                            platform="fake")

    async def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, message_id="1")


def test_telegram_webhook_round_trip(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())  # ask() -> "full"
    ch = _FakeChannel()
    with TestClient(create_app(hive, telegram=ch)) as c:
        r = c.post("/telegram/webhook", json={"message": {"text": "hi there"}})
        assert r.status_code == 200 and r.json()["handled"] is True
        assert ch.sent and ch.sent[0].chat_id == "42"


def test_telegram_webhook_ignores_nonactionable(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())
    ch = _FakeChannel()
    with TestClient(create_app(hive, telegram=ch)) as c:
        r = c.post("/telegram/webhook", json={"edited_message": {"text": "x"}})
        assert r.json()["handled"] is False
        assert not ch.sent


# --- ask_stream session history -----------------------------------------------

class _CapturingStreamRouter:
    """Records the messages list passed to stream() for assertion."""

    def __init__(self):
        self.captured_messages = None

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="ok", model="m")

    async def stream(self, messages, *, system=None, **kw):
        self.captured_messages = list(messages)
        yield "token"

    async def aclose(self):
        pass


def test_ask_stream_includes_session_history(tmp_path):
    router = _CapturingStreamRouter()
    hive = HiveOS.build(_config(tmp_path), router=router)
    # seed the session with prior turns
    hive.session_store.append("s-hist", Role.USER, "prior question")
    hive.session_store.append("s-hist", Role.ASSISTANT, "prior answer")

    async def collect():
        return [tok async for tok in hive.ask_stream("new question", session_id="s-hist")]

    asyncio.run(collect())
    assert router.captured_messages is not None
    contents = [m.content for m in router.captured_messages]
    assert any("prior question" in c for c in contents), "history should be included"
    assert any("new question" in c for c in contents), "current message should be included"


def test_ask_stream_works_with_no_prior_history(tmp_path):
    router = _CapturingStreamRouter()
    hive = HiveOS.build(_config(tmp_path), router=router)

    async def collect():
        return [tok async for tok in hive.ask_stream("hello", session_id="fresh")]

    tokens = asyncio.run(collect())
    assert tokens == ["token"]
    assert router.captured_messages is not None
