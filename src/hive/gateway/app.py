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

    @app.get("/budget/detail", dependencies=[Depends(require_token)])
    async def budget_detail() -> dict:
        snap = hive.budgeter.snapshot()
        return {**snap,
                "remaining_calls": hive.budgeter.remaining_calls(),
                "is_near_cap": hive.budgeter.is_near_cap()}

    @app.get("/budget/forecast", dependencies=[Depends(require_token)])
    async def budget_forecast() -> dict:
        """Return capacity forecast: pct used today, remaining calls, days estimate."""
        return hive.budgeter.forecast()

    @app.get("/system-status", dependencies=[Depends(require_token)])
    async def system_status() -> dict:
        """Full system status: router config, budget forecast, memory, tasks, tools."""
        return hive.system_status()

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

    @app.get("/tools/stats", dependencies=[Depends(require_token)])
    async def tools_stats() -> dict:
        return hive.tool_executor.stats()

    @app.post("/memory/{session_id}/consolidate", dependencies=[Depends(require_token)])
    async def memory_consolidate(session_id: str) -> dict:
        """Run the memory-keeper consolidation for a session (aux-model call).
        Extracts durable learnings from recent episodic turns and saves them to memory."""
        count = await hive.consolidate(session_id)
        return {"session_id": session_id, "new_items": count,
                "last_ts": hive.keeper.last_consolidated_ts}

    @app.get("/memory/session/{session_id}/count", dependencies=[Depends(require_token)])
    async def memory_session_count(session_id: str) -> dict:
        """Return the number of episodic turns stored for a session."""
        count = 0
        if hasattr(hive.memory, "count_episodic"):
            count = hive.memory.count_episodic(session_id)
        return {"session_id": session_id, "episodic_count": count}

    @app.delete("/memory/session/{session_id}", dependencies=[Depends(require_token)])
    async def memory_session_delete(session_id: str) -> dict:
        """Delete all episodic memory turns for a session."""
        deleted = 0
        if hasattr(hive.memory, "delete_session_memory"):
            deleted = hive.memory.delete_session_memory(session_id)
        return {"session_id": session_id, "deleted": deleted}

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

    @app.get("/traces", dependencies=[Depends(require_token)])
    async def traces_list() -> dict:
        return {"sessions": hive.traces.sessions()}

    @app.get("/traces/{session_id}", dependencies=[Depends(require_token)])
    async def traces(session_id: str = "default") -> dict:
        return {"session_id": session_id, "events": hive.traces.export(session_id),
                "event_count": hive.traces.event_count(session_id),
                "sessions": hive.traces.sessions()}

    @app.delete("/traces/{session_id}", dependencies=[Depends(require_token)])
    async def traces_clear(session_id: str) -> dict:
        count = hive.traces.clear(session_id)
        return {"cleared": count, "session_id": session_id}

    @app.get("/audit", dependencies=[Depends(require_token)])
    async def audit(limit: int = 50) -> dict:
        return {"entries": hive.audit_log.recent(limit=min(limit, 200))}

    @app.get("/audit/stats", dependencies=[Depends(require_token)])
    async def audit_stats() -> dict:
        """Return audit summary grouped by tool and status."""
        return hive.audit_log.stats()

    @app.get("/audit/search", dependencies=[Depends(require_token)])
    async def audit_search(tool: str | None = None, status: str | None = None,
                           limit: int = 50) -> dict:
        """Search audit log by tool name and/or status."""
        entries = hive.audit_log.search(tool=tool, status=status,
                                        limit=min(limit, 200))
        return {"entries": entries, "count": len(entries)}

    @app.get("/skills", dependencies=[Depends(require_token)])
    async def skills_list() -> dict:
        """Return skill usage statistics."""
        return hive.skill_usage.stats()

    @app.delete("/audit/purge", dependencies=[Depends(require_token)])
    async def audit_purge(max_age_days: float = 90.0) -> dict:
        """Delete audit entries older than max_age_days. Returns count purged."""
        deleted = hive.audit_log.purge_old(max_age_days=max_age_days)
        return {"deleted": deleted, "max_age_days": max_age_days}

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

    @app.get("/tasks/by-kind", dependencies=[Depends(require_token)])
    async def tasks_by_kind() -> dict:
        return {"by_kind": hive.task_board.count_by_kind()}

    @app.get("/tasks/stats", dependencies=[Depends(require_token)])
    async def tasks_stats() -> dict:
        return hive.task_board.statistics()

    @app.get("/tasks/failed", dependencies=[Depends(require_token)])
    async def tasks_failed(limit: int = 10) -> dict:
        """Return the most recently failed tasks."""
        items = hive.task_board.recent_failures(limit=min(limit, 100))
        return {"tasks": [{"id": t.id, "kind": t.kind, "source": t.source,
                           "attempts": t.attempts, "last_error": t.last_error,
                           "updated_ts": t.updated_ts} for t in items]}

    @app.post("/tasks/retry-failed", dependencies=[Depends(require_token)])
    async def tasks_retry_failed() -> dict:
        """Bulk-retry all failed tasks, resetting them to pending."""
        count = hive.task_board.retry_all_failed()
        return {"retried": count}

    @app.post("/tasks/bulk-cancel", dependencies=[Depends(require_token)])
    async def tasks_bulk_cancel(body: dict | None = None) -> dict:
        """Cancel all PENDING tasks, optionally filtered by kind."""
        kind = (body or {}).get("kind")
        count = hive.task_board.bulk_cancel_pending(kind=kind or None)
        return {"cancelled": count, "kind": kind}

    @app.post("/tasks/requeue-running", dependencies=[Depends(require_token)])
    async def tasks_requeue_running() -> dict:
        """Reset all RUNNING tasks back to PENDING (crash-recovery after unclean shutdown)."""
        return hive.resume_after_restart()

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

    @app.get("/sessions/stats", dependencies=[Depends(require_token)])
    async def sessions_stats() -> dict:
        return hive.session_store.stats()

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

    @app.post("/sessions/{session_id}/auto-title", dependencies=[Depends(require_token)])
    async def session_auto_title(session_id: str) -> dict:
        """Generate a short title for the session from its first message (best-effort)."""
        title = await hive.title_session(session_id)
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

    @app.get("/cron/{job_id}", dependencies=[Depends(require_token)])
    async def cron_get(job_id: int) -> dict:
        job = hive.cron.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="cron job not found")
        return {"id": job.id, "schedule": job.schedule, "task_kind": job.task_kind,
                "payload": job.payload, "enabled": job.enabled,
                "last_run": job.last_run, "next_run": job.next_run}

    @app.get("/commitments", dependencies=[Depends(require_token)])
    async def commitments_list(active_only: bool = False) -> dict:
        items = hive.commitments.all(active_only=active_only)
        return {"commitments": [
            {"id": c.id, "description": c.description,
             "cadence_seconds": c.cadence_seconds, "task_kind": c.task_kind,
             "active": c.active, "last_fulfilled": c.last_fulfilled,
             "created_ts": c.created_ts}
            for c in items
        ]}

    @app.post("/commitments", dependencies=[Depends(require_token)])
    async def commitments_add(body: dict) -> dict:
        description = body.get("description", "")
        cadence = body.get("cadence_seconds")
        if not description or cadence is None:
            raise HTTPException(status_code=422,
                                detail="description and cadence_seconds are required")
        cid = hive.commitments.add(
            description, float(cadence),
            task_kind=body.get("task_kind", "commitment"),
            payload=body.get("payload"),
        )
        return {"id": cid, "description": description, "cadence_seconds": cadence}

    @app.delete("/commitments/{commitment_id}", dependencies=[Depends(require_token)])
    async def commitments_remove(commitment_id: int) -> dict:
        ok = hive.commitments.remove(commitment_id)
        if not ok:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"removed": True, "commitment_id": commitment_id}

    @app.post("/commitments/{commitment_id}/fulfill", dependencies=[Depends(require_token)])
    async def commitments_fulfill(commitment_id: int) -> dict:
        ok = hive.commitments.fulfill(commitment_id)
        if not ok:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"fulfilled": True, "commitment_id": commitment_id}

    @app.get("/commitments/overdue", dependencies=[Depends(require_token)])
    async def commitments_overdue() -> dict:
        items = hive.commitments.overdue()
        return {"overdue": [
            {"id": c.id, "description": c.description,
             "cadence_seconds": c.cadence_seconds, "last_fulfilled": c.last_fulfilled}
            for c in items
        ]}

    @app.get("/approvals/edits", dependencies=[Depends(require_token)])
    async def approvals_edits() -> dict:
        """List all pending REVIEW-tier self-mod edits awaiting human decision."""
        return {"pending_edits": hive.pending_review_edits(),
                "count": hive.improver.pending_count()}

    @app.delete("/approvals/cancel-all", dependencies=[Depends(require_token)])
    async def approvals_cancel_all() -> dict:
        """Cancel ALL pending REVIEW-tier self-mod edits at once."""
        count = hive.abort_all_self_mods()
        return {"cancelled": count}

    @app.get("/model/catalog", dependencies=[Depends(require_token)])
    async def model_catalog() -> dict:
        """List all registered model IDs in the model catalog."""
        catalog = getattr(hive.router, "_catalog", None)
        if catalog is None:
            return {"models": [], "count": 0}
        return {"models": catalog.list_models(), "count": len(catalog)}

    @app.post("/run-tests", dependencies=[Depends(require_token)])
    async def run_tests_endpoint(dry_run: bool = False) -> dict:
        """Run the project test suite and return structured results.
        Safe to call at any time — never triggers self-modification.
        Use POST /self-diagnose to run tests AND trigger improvements."""
        if dry_run:
            return {"all_passed": True, "passed": 0, "failed": 0, "errors": 0,
                    "skipped": 0, "timed_out": False, "dry_run": True, "output": ""}
        return await hive.run_tests()

    @app.get("/traces/export/{session_id}", dependencies=[Depends(require_token)])
    async def traces_export(session_id: str) -> dict:
        """Export a session's event trace as a structured, JSON-serialisable list."""
        events = hive.traces.export(session_id)
        return {"session_id": session_id, "events": events, "count": len(events)}

    @app.get("/self-improve/history", dependencies=[Depends(require_token)])
    async def self_improve_history(limit: int = 20) -> dict:
        """Return the most recent self-mod proposal outcomes (newest first)."""
        records = hive.self_modifier.history(limit=max(1, min(limit, 100)))
        return {"history": records, "count": len(records)}

    @app.get("/self-improve/status", dependencies=[Depends(require_token)])
    async def self_improve_status() -> dict:
        """Comprehensive self-improvement system status snapshot."""
        return {
            "pending_review_count": hive.improver.pending_count(),
            "pending_review": hive.improver.describe_pending(),
            "recent_branches": hive.self_modifier.recent_branches(n=5),
            "last_result": hive.self_modifier.last_result,
            "history_count": len(hive.self_modifier.history(limit=1000)),
        }

    @app.get("/self-improve/pending", dependencies=[Depends(require_token)])
    async def self_improve_pending() -> dict:
        """Return detailed metadata for all pending REVIEW-tier edits."""
        pending = hive.improver.describe_pending()
        return {"pending": pending, "count": len(pending)}

    @app.post("/self-improve/symptom", dependencies=[Depends(require_token)])
    async def self_improve_symptom(body: dict) -> dict:
        """Trigger a symptom-based self-improvement cycle without running tests first.
        The LLM diagnoser analyses the symptom and proposes typed edits; AUTO tier
        opens a draft PR, REVIEW tier queues for human approval."""
        symptom = body.get("symptom", "").strip()
        if not symptom:
            raise HTTPException(status_code=422, detail="symptom is required")
        outcomes = await hive.self_improve_from_symptom(symptom)
        return {"outcomes": [
            {"status": o.status, "op": o.op.value, "tier": o.tier.value,
             "detail": o.detail, "branch": o.branch, "approval_id": o.approval_id}
            for o in outcomes
        ]}

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
