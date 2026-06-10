"""
app.py — HiveOS gateway (FastAPI, KEEP+IMPROVE from Gateway/app.py).

`create_app(hive)` builds the app around an assembled HiveOS, so the gateway holds
no globals and is trivially testable with Starlette's TestClient. Surfaces
(terminal/dashboard/voice/telegram) reach Hive through:
  GET  /health                 — liveness
  POST /chat                   — one turn (auth)
  WS   /ws                     — streaming-ish chat loop (token handshake)
  GET  /budget                 — budgeter snapshot (auth)
  GET  /approvals              — pending danger-gated calls (auth)
  POST /approvals/decide       — approve/deny; approval runs the gated tool (auth)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from fastapi import Request

from hive.core.approval import gate
from hive.gateway.auth import make_auth_dependency, token_ok
from hive.gateway.channels.base import ChannelAdapter, OutgoingMessage
from hive.gateway.channels.telegram import TelegramChannel
from hive.gateway.protocol import ApprovalDecision, ChatRequest, ChatResponse
from hive.runtime import HiveOS

log = logging.getLogger("hive.gateway")


def create_app(hive: HiveOS, *, telegram: ChannelAdapter | None = None) -> FastAPI:
    secret = hive.config.secret
    require_token = make_auth_dependency(secret)
    # Telegram surface (optional): use an injected channel, else build one from config.
    if telegram is None and hive.config.telegram_token:
        telegram = TelegramChannel(hive.config.telegram_token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hive.load_mcp_servers()   # connect configured MCP servers (best-effort, A2)
        log.info("HiveOS gateway online")
        yield
        await hive.aclose()
        log.info("HiveOS gateway offline")

    app = FastAPI(title="HiveOS Gateway", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "hiveos-gateway"}

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
    async def chat(body: ChatRequest) -> ChatResponse:
        reply = await hive.ask(body.message, session_id=body.session_id)
        return ChatResponse(reply=reply, session_id=body.session_id)

    @app.post("/chat/stream", dependencies=[Depends(require_token)])
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        """SSE token stream of a conversational reply (M4 #sf-1). Each token is one
        `data:` event; the stream ends with `data: [DONE]`."""
        async def events():
            try:
                async for delta in hive.ask_stream(body.message, session_id=body.session_id):
                    yield f"data: {delta}\n\n"
            except Exception as exc:  # noqa: BLE001 - surface as a terminal SSE error
                log.warning("stream error: %s", exc)
                yield f"event: error\ndata: {exc}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/budget", dependencies=[Depends(require_token)])
    async def budget() -> dict:
        return hive.budgeter.snapshot()

    @app.get("/approvals", dependencies=[Depends(require_token)])
    async def approvals() -> dict:
        return {"pending": gate.pending()}

    @app.post("/approvals/decide", dependencies=[Depends(require_token)])
    async def decide(body: ApprovalDecision) -> dict:
        item = gate.resolve(body.approval_id, body.approved)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        if not body.approved:
            return {"executed": False}
        dispatch = await hive.tool_executor.execute_approved(item["tool"], item["args"])
        return {"executed": True, "status": dispatch.status.value,
                "result": dispatch.result.content if dispatch.result else None,
                "error": dispatch.error}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        token = await websocket.receive_text()
        if not token_ok(token, secret):
            await websocket.send_json({"type": "error", "data": "unauthorized"})
            await websocket.close()
            return
        try:
            while True:
                user_msg = await websocket.receive_text()
                reply = await hive.ask(user_msg, session_id="ws")
                await websocket.send_json({"type": "reply", "data": reply})
        except WebSocketDisconnect:
            log.info("ws client disconnected")

    if telegram is not None:
        webhook_secret = hive.config.telegram_webhook_secret

        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request) -> dict:
            # Telegram authenticates webhooks via this header (set at setWebhook time).
            if webhook_secret and request.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token") != webhook_secret:
                raise HTTPException(status_code=401, detail="bad webhook secret")
            update = await request.json()
            event = telegram.parse_update(update)
            if event is None:
                return {"ok": True, "handled": False}  # nothing actionable
            reply = await hive.ask(event.text, session_id=f"telegram:{event.chat_id}")
            await telegram.send(OutgoingMessage(chat_id=event.chat_id, text=reply,
                                                reply_to=event.message_id or None))
            return {"ok": True, "handled": True}

    return app
