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

    @app.get("/health/full", dependencies=[Depends(require_token)])
    async def health_full() -> dict:
        """Full system health snapshot including budget, tasks, memory, and telemetry."""
        return hive.health()

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
    async def chat(body: ChatRequest) -> ChatResponse:
        try:
            reply = await hive.ask(body.message, session_id=body.session_id)
        except Exception as exc:  # noqa: BLE001
            log.error("chat turn failed (session=%s): %s", body.session_id, exc, exc_info=True)
            raise HTTPException(status_code=503, detail="internal error") from exc
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
                log.error("stream error (session=%s): %s", body.session_id, exc, exc_info=True)
                safe_name = type(exc).__name__.replace("\n", " ").replace("\r", " ")
                yield f"event: error\ndata: {safe_name}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/budget", dependencies=[Depends(require_token)])
    async def budget() -> dict:
        return hive.budgeter.snapshot()

    @app.get("/config/validate", dependencies=[Depends(require_token)])
    async def config_validate() -> dict:
        issues = hive.config.validate()
        return {"valid": len(issues) == 0, "issues": issues}

    @app.get("/tools", dependencies=[Depends(require_token)])
    async def tools_list() -> dict:
        return {
            "count": len(hive.tools),
            "tools": [
                {"name": t.spec.name, "category": t.spec.category,
                 "description": t.spec.description, "dangerous": t.spec.dangerous,
                 "available": t.available()}
                for t in hive.tools.values()
            ],
        }

    @app.get("/memory/export", dependencies=[Depends(require_token)])
    async def memory_export() -> dict:
        if hasattr(hive.memory, "export_backup"):
            return hive.memory.export_backup()
        return {"knowledge": [], "episodic": [],
                "knowledge_count": 0, "episodic_count": 0,
                "note": "export not supported by this memory provider"}

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

    @app.get("/audit/stats", dependencies=[Depends(require_token)])
    async def audit_stats() -> dict:
        """Return audit summary grouped by tool and status."""
        return hive.audit_log.stats()

    @app.get("/audit/export", dependencies=[Depends(require_token)])
    async def audit_export(start_ts: float | None = None,
                           end_ts: float | None = None) -> dict:
        """Export audit entries for a time range (UNIX timestamps). Omit params for all."""
        entries = hive.audit_log.export(start_ts=start_ts, end_ts=end_ts)
        return {"entries": entries, "count": len(entries)}

    @app.get("/tasks", dependencies=[Depends(require_token)])
    async def tasks(kind: str | None = None, source: str | None = None,
                    state: str | None = None) -> dict:
        if kind is not None or source is not None or state is not None:
            found = hive.task_board.search(kind=kind, source=source, state=state)
            return {
                "pending": hive.task_board.pending_count(),
                "tasks": [
                    {"id": t.id, "kind": t.kind, "state": t.state,
                     "source": t.source, "attempts": t.attempts,
                     "last_error": t.last_error, "created_ts": t.created_ts,
                     "payload": t.payload}
                    for t in found
                ],
            }
        recent = hive.task_board.all()[-20:]  # last 20 across all states
        return {
            "pending": hive.task_board.pending_count(),
            "tasks": [
                {"id": t.id, "kind": t.kind, "state": t.state,
                 "source": t.source, "attempts": t.attempts,
                 "last_error": t.last_error, "created_ts": t.created_ts,
                 "payload": t.payload}
                for t in reversed(recent)  # newest first
            ],
        }

    @app.get("/tasks/stats", dependencies=[Depends(require_token)])
    async def tasks_stats() -> dict:
        return hive.task_board.statistics()

    @app.post("/tasks/retry-failed", dependencies=[Depends(require_token)])
    async def tasks_retry_failed() -> dict:
        """Bulk-retry all failed tasks, resetting them to pending."""
        count = hive.task_board.retry_all_failed()
        return {"retried": count}

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_token)])
    async def task_get(task_id: int) -> dict:
        task = hive.task_board.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"id": task.id, "kind": task.kind, "state": task.state,
                "source": task.source, "attempts": task.attempts,
                "last_error": task.last_error, "created_ts": task.created_ts,
                "payload": task.payload}

    @app.post("/tasks/{task_id}/retry", dependencies=[Depends(require_token)])
    async def task_retry(task_id: int) -> dict:
        ok = hive.task_board.retry(task_id)
        if not ok:
            raise HTTPException(status_code=409, detail="task is not in failed state")
        return {"retried": True, "task_id": task_id}

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
    async def task_cancel(task_id: int) -> dict:
        ok = hive.task_board.cancel(task_id)
        if not ok:
            raise HTTPException(status_code=409, detail="task is not in pending state")
        return {"cancelled": True, "task_id": task_id}

    @app.get("/sessions", dependencies=[Depends(require_token)])
    async def sessions_list() -> dict:
        return {"sessions": hive.session_store.list_sessions()}

    @app.get("/sessions/search", dependencies=[Depends(require_token)])
    async def sessions_search(q: str, session_id: str | None = None,
                              limit: int = 10) -> dict:
        results = hive.session_store.search(q, session_id=session_id,
                                            limit=min(limit, 100))
        return {"results": results, "count": len(results)}

    @app.get("/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def session_get(session_id: str) -> dict:
        msg_count = hive.session_store.count_messages(session_id)
        title = hive.session_store.get_title(session_id)
        summary = hive.session_store.get_summary(session_id)
        return {"session_id": session_id, "message_count": msg_count,
                "title": title, "summary": summary}

    @app.get("/sessions/{session_id}/title", dependencies=[Depends(require_token)])
    async def session_get_title(session_id: str) -> dict:
        title = hive.session_store.get_title(session_id)
        return {"session_id": session_id, "title": title}

    @app.post("/sessions/{session_id}/title", dependencies=[Depends(require_token)])
    async def session_set_title(session_id: str, body: dict) -> dict:
        title = body.get("title", "")
        if not title:
            raise HTTPException(status_code=422, detail="title is required")
        hive.session_store.ensure(session_id)
        hive.session_store.set_title(session_id, title)
        return {"session_id": session_id, "title": title}

    @app.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def session_delete(session_id: str) -> dict:
        deleted = hive.session_store.delete_session(session_id)
        return {"deleted": deleted, "session_id": session_id}

    @app.get("/cron", dependencies=[Depends(require_token)])
    async def cron_list() -> dict:
        jobs = hive.cron.jobs()
        return {"jobs": [
            {"id": j.id, "schedule": j.schedule, "task_kind": j.task_kind,
             "payload": j.payload, "enabled": j.enabled,
             "last_run": j.last_run, "next_run": j.next_run}
            for j in jobs
        ]}

    @app.post("/cron", dependencies=[Depends(require_token)])
    async def cron_add(body: dict) -> dict:
        schedule = body.get("schedule", "")
        task_kind = body.get("task_kind", "")
        if not schedule or not task_kind:
            raise HTTPException(status_code=422, detail="schedule and task_kind are required")
        job_id = hive.cron.add(schedule, task_kind, body.get("payload"),
                               enabled=body.get("enabled", True))
        return {"id": job_id, "schedule": schedule, "task_kind": task_kind}

    @app.post("/cron/{job_id}/enable", dependencies=[Depends(require_token)])
    async def cron_enable(job_id: int) -> dict:
        hive.cron.set_enabled(job_id, True)
        return {"enabled": True, "job_id": job_id}

    @app.post("/cron/{job_id}/disable", dependencies=[Depends(require_token)])
    async def cron_disable(job_id: int) -> dict:
        hive.cron.set_enabled(job_id, False)
        return {"enabled": False, "job_id": job_id}

    @app.delete("/cron/{job_id}", dependencies=[Depends(require_token)])
    async def cron_remove(job_id: int) -> dict:
        ok = hive.cron.remove(job_id)
        if not ok:
            raise HTTPException(status_code=404, detail="cron job not found")
        return {"removed": True, "job_id": job_id}

    @app.post("/self-diagnose", dependencies=[Depends(require_token)])
    async def self_diagnose_endpoint(dry_run: bool = False) -> dict:
        """Run the test suite and trigger a self-improvement cycle for any failures.
        Hive never auto-merges: AUTO tier edits open draft PRs, REVIEW tier goes to
        /approvals for human decision. Safe to call at any time."""
        return await hive.self_diagnose(dry_run=dry_run)

    @app.get("/approvals", dependencies=[Depends(require_token)])
    async def approvals() -> dict:
        return {"pending": gate.pending(),
                "pending_edits": hive.improver.pending_count()}

    @app.post("/approvals/cancel", dependencies=[Depends(require_token)])
    async def approvals_cancel(body: ApprovalDecision) -> dict:
        """Cancel a pending REVIEW-tier self-mod edit without applying it."""
        removed = hive.improver.cancel_review(body.approval_id)
        if not removed:
            raise HTTPException(status_code=404, detail="pending edit not found")
        hive.edit_pending.pop(body.approval_id, None)
        return {"cancelled": True, "approval_id": body.approval_id}

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
                    await websocket.send_json({"type": "error", "data": "internal error"})
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
            try:
                update = await request.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram webhook: failed to parse request body: %s", exc)
                return {"ok": True, "handled": False}
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
                # Return 500 so Telegram retries transient failures (LLM outage, timeout).
                # Telegram backs off and eventually stops; permanent errors are logged above.
                raise HTTPException(status_code=500, detail="internal error") from exc
            return {"ok": True, "handled": True}

    return app
