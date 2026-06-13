"""
app.py — HiveOS gateway (FastAPI, KEEP+IMPROVE from Gateway/app.py).

`create_app(hive)` builds the app around an assembled HiveOS, so the gateway holds
no globals and is trivially testable with Starlette's TestClient. Surfaces
(terminal/dashboard/voice/telegram) reach Hive through:
  GET  /health                 — liveness
  POST /chat                   — one turn (auth)
  POST /chat/stream            — SSE token stream (auth, M4 #sf-1)
  WS   /ws                     — streaming-ish chat loop (token handshake)
  GET  /budget                 — budgeter snapshot (auth)
  GET  /telemetry              — model/token/cost counters (auth, M10-a)
  GET  /traces/{session_id}    — per-session event trace (auth, M10-a)
  GET  /audit                  — recent tool-call audit entries (auth, M10-a)
  GET  /tasks                  — task board state (auth, M10-a)
  GET  /approvals              — pending danger-gated calls (auth)
  POST /approvals/decide       — approve/deny; approval runs the gated tool (auth)
  GET  /app/*                  — Mission Control dashboard SPA (if dashboard/dist built)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from hive.core.approval import gate
from hive.gateway.auth import make_auth_dependency, token_ok
from hive.gateway.channels.base import ChannelAdapter, OutgoingMessage
from hive.gateway.channels.telegram import TelegramChannel
from hive.gateway.protocol import ApprovalDecision, ChatRequest, ChatResponse
from hive.runtime import HiveOS

# Dashboard dist path: src/hive/gateway/ → repo root / dashboard/dist
_DASHBOARD_DIST = Path(__file__).parent.parent.parent.parent / "dashboard" / "dist"

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
        from hive.gateway.protocol import PROTOCOL_VERSION
        return {"status": "ok", "service": "hiveos-gateway",
                "protocol_version": PROTOCOL_VERSION}

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
    async def chat(body: ChatRequest) -> ChatResponse:
        try:
            reply = await hive.ask(body.message, session_id=body.session_id)
        except Exception as exc:  # noqa: BLE001
            log.error("chat turn failed (session=%s): %s", body.session_id, exc, exc_info=True)
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc
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
                yield f"event: error\ndata: {type(exc).__name__}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/budget", dependencies=[Depends(require_token)])
    async def budget() -> dict:
        return hive.budgeter.snapshot()

    @app.get("/telemetry", dependencies=[Depends(require_token)])
    async def telemetry() -> dict:
        return hive.telemetry.snapshot()

    @app.get("/traces/{session_id}", dependencies=[Depends(require_token)])
    async def traces(session_id: str = "default") -> dict:
        return {"session_id": session_id, "events": hive.traces.export(session_id),
                "sessions": hive.traces.sessions()}

    @app.get("/audit", dependencies=[Depends(require_token)])
    async def audit(limit: int = 50) -> dict:
        return {"entries": hive.audit_log.recent(limit=min(limit, 200))}

    @app.get("/tasks", dependencies=[Depends(require_token)])
    async def tasks() -> dict:
        recent = hive.task_board.all()[-20:]  # last 20 across all states
        return {
            "pending": hive.task_board.pending_count(),
            "tasks": [
                {"id": t.id, "kind": t.kind, "state": t.state,
                 "source": t.source, "attempts": t.attempts,
                 "last_error": t.last_error, "created_ts": t.created_ts}
                for t in reversed(recent)  # newest first
            ],
        }

    @app.get("/approvals", dependencies=[Depends(require_token)])
    async def approvals() -> dict:
        return {"pending": gate.pending()}

    @app.post("/approvals/decide", dependencies=[Depends(require_token)])
    async def decide(body: ApprovalDecision) -> dict:
        item = gate.resolve(body.approval_id, body.approved)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        if not body.approved:
            hive.edit_pending.pop(body.approval_id, None)
            return {"executed": False}
        # Self-mod REVIEW-tier edit: route to the self-modifier, not the tool executor.
        if str(item.get("tool", "")).startswith("self_mod:"):
            edit = hive.edit_pending.pop(body.approval_id, None)
            if edit is None:
                return {"executed": False,
                        "error": "edit not found (process may have restarted)"}
            outcome = await hive.improver.apply_approved(edit)
            return {"executed": True, "status": outcome.status,
                    "branch": outcome.branch, "detail": outcome.detail}
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
                try:
                    reply = await hive.ask(user_msg, session_id="ws")
                    await websocket.send_json({"type": "reply", "data": reply})
                except Exception as exc:  # noqa: BLE001
                    log.error("ws turn error: %s", exc, exc_info=True)
                    await websocket.send_json({"type": "error",
                                               "data": f"{type(exc).__name__}: {exc}"})
        except WebSocketDisconnect:
            log.info("ws client disconnected")

    # Serve the Mission Control dashboard SPA if it has been built (opt-in).
    # Mount at /app so API routes take priority; `npm run build` in dashboard/ to enable.
    if _DASHBOARD_DIST.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/app", StaticFiles(directory=str(_DASHBOARD_DIST), html=True),
                  name="dashboard")
        log.info("Mission Control dashboard served at /app")

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
            try:
                reply = await hive.ask(event.text, session_id=f"telegram:{event.chat_id}")
                await telegram.send(OutgoingMessage(chat_id=event.chat_id, text=reply,
                                                    reply_to=event.message_id or None))
            except Exception as exc:  # noqa: BLE001
                log.error("telegram turn failed (chat=%s): %s", event.chat_id, exc,
                          exc_info=True)
                # Return 200 to Telegram to stop retries; failure is logged.
                return {"ok": False, "handled": False, "error": type(exc).__name__}
            return {"ok": True, "handled": True}

    return app
