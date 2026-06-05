"""
app.py — HiveOS Gateway Daemon (FastAPI, 24/7).

Surfaces (terminal, dashboard, voice, Telegram) reach Hive through here.

Run:  uvicorn gateway.app:app --host 0.0.0.0 --port 8088
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core import settings, session
from core.model_router import router, TaskKind
from core.approval_gate import gate
from core.budgeter import budgeter
from tools import registry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hiveos.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("HiveOS gateway online")
    yield
    await router.aclose()
    log.info("HiveOS gateway offline")


app = FastAPI(title="HiveOS Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _auth(token: str | None) -> None:
    if token != settings.HIVE_SECRET:
        raise HTTPException(status_code=401, detail="bad token")


class ChatIn(BaseModel):
    session_id: str = "default"
    message: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "hiveos-gateway"}


@app.get("/budget")
async def budget(x_hive_token: str | None = Header(default=None)) -> dict:
    _auth(x_hive_token)
    return {"remaining_pct": await budgeter.remaining_pct(),
            "calls_today": budgeter._calls_today,
            "daily_cap": settings.DAILY_CALL_CAP}


@app.post("/chat")
async def chat(body: ChatIn, x_hive_token: str | None = Header(default=None)) -> dict:
    _auth(x_hive_token)
    system, msgs = session.build(body.message, body.session_id)
    reply = await router.complete(msgs, kind=TaskKind.EXECUTE, system=system)
    session.record("user", body.message, body.session_id)
    session.record("assistant", reply, body.session_id)
    return {"reply": reply}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    token = await websocket.receive_text()
    if token != settings.HIVE_SECRET:
        await websocket.send_json({"type": "error", "data": "unauthorized"})
        await websocket.close()
        return
    sid = "default"
    try:
        while True:
            user_msg = await websocket.receive_text()
            system, msgs = session.build(user_msg, sid)
            reply = await router.complete(msgs, kind=TaskKind.EXECUTE, system=system)
            session.record("user", user_msg, sid)
            session.record("assistant", reply, sid)
            await websocket.send_json({"type": "reply", "data": reply})
    except WebSocketDisconnect:
        log.info("client disconnected")


@app.get("/approvals")
async def approvals(x_hive_token: str | None = Header(default=None)) -> dict:
    _auth(x_hive_token)
    return {"pending": gate.pending()}


class Decision(BaseModel):
    approval_id: str
    approved: bool


@app.post("/approvals/decide")
async def decide(body: Decision, x_hive_token: str | None = Header(default=None)) -> dict:
    _auth(x_hive_token)
    item = gate.resolve(body.approval_id, body.approved)
    if not item:
        raise HTTPException(status_code=404, detail="unknown approval")
    if body.approved:
        return {"executed": True, "result": await registry.execute_approved(item)}
    return {"executed": False}
