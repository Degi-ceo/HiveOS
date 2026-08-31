# HiveOS Gateway — API Reference

The HiveOS gateway is a FastAPI application built by `gateway/app.py:create_app(hive)`.
It runs on `HIVE_HOST:HIVE_PORT` (default `0.0.0.0:8088`).

**Authentication:** All endpoints except `/health` and `/ws` require a Bearer token:

```
Authorization: Bearer <HIVE_SECRET>
```

---

## Authentication

All endpoints except `GET /health` require a Bearer token in the `Authorization` header:

```
Authorization: Bearer <HIVE_SECRET>
```

`HIVE_SECRET` is set in `.env` (default `change_me` — change before exposing publicly).
Token comparison uses `hmac.compare_digest` (constant-time, no timing attacks).

**401 causes:**
- Missing `Authorization` header
- Wrong token value
- Token is empty string or the unset default `change_me` on a hardened deployment

**WebSocket exception:** `/ws` uses a first-frame token exchange instead of HTTP headers (see below).

---

## Health

### `GET /health`

Liveness probe. No authentication required.

**Response**
```json
{
  "status": "ok",
  "service": "hiveos-gateway",
  "protocol_version": "1.0"
}
```

`protocol_version` follows additive-first versioning — new fields are added without breaking old clients.

**curl**
```bash
curl http://localhost:8088/health
```

---

## OpenAI-compatible endpoints

These endpoints accept the OpenAI `chat/completions` wire format so Hive can act as a
drop-in model provider for any OpenAI SDK client (Cursor, Continue, Aider, etc.).

### `GET /v1/models`

Returns the list of available models in OpenAI format.

**Response**
```json
{
  "object": "list",
  "data": [{"id": "hive", "object": "model", "created": 0, "owned_by": "hiveos"}]
}
```

### `POST /v1/chat/completions`

OpenAI-format chat completion. Supports both non-streaming and SSE streaming.

**Request**
```json
{
  "model": "hive",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}
```

**Non-streaming response**
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "hive",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}]
}
```

**Streaming response** (`stream: true`): Server-Sent Events, each chunk is
`data: {"choices":[{"delta":{"content":"..."}}]}\n\n`, terminated by `data: [DONE]`.

**curl (non-streaming)**
```bash
curl -X POST http://localhost:8088/v1/chat/completions \
  -H "Authorization: Bearer $HIVE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"model":"hive","messages":[{"role":"user","content":"ping"}]}'
```

---

## Chat

### `POST /chat`

One-shot conversational turn. Returns the full reply after the model finishes.

**Request**
```json
{
  "message": "What is the current task queue state?",
  "session_id": "default"
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `message` | string | required | User message |
| `session_id` | string | `"default"` | Identifies the conversation; session history is stored per ID |

**Response**
```json
{
  "reply": "There are 3 pending tasks ...",
  "session_id": "default",
  "protocol_version": "1.0"
}
```

**curl**
```bash
curl -s -X POST http://localhost:8088/chat \
  -H "Authorization: Bearer $HIVE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "say hello", "session_id": "default"}' | jq .
```

---

### `POST /chat/stream`

Server-Sent Events token stream. Each token is delivered immediately as it is generated.
Useful for the dashboard chat pane and any UI that should render progressively.

**Request** — same shape as `POST /chat`

**Response** — `Content-Type: text/event-stream`

```
data: Hello

data: , here

data:  is the answer

data: [DONE]
```

Each `data:` line is one token delta. The stream ends with `data: [DONE]`. On error, a
final event is emitted before the stream closes:

```
event: error
data: TimeoutError
```

Only the exception class name is emitted — never the message or stacktrace.

**curl**
```bash
curl -s -N -X POST http://localhost:8088/chat/stream \
  -H "Authorization: Bearer $HIVE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "tell me a short story"}'
# tokens print as they arrive; stream ends with "data: [DONE]"
```

---

### `WS /ws`

WebSocket chat loop. Authenticate by sending the bearer token as the first text frame.

**Handshake**
```
→ (text frame)  "my-secret-token"
← (json frame)  {"type": "error", "data": "unauthorized"}   # if bad token, then close
```

**Per-turn**
```
→ (text frame)  "your message here"
← (json frame)  {"type": "reply", "data": "the reply text"}
```

Session ID is fixed to `"ws"` for all WebSocket connections.

---

## Budget

### `GET /budget`

Current budgeter snapshot: calls made today, tokens consumed, and rolling window.

**Response**
```json
{
  "calls_today": 42,
  "daily_cap": 3000,
  "remaining_window_pct": 65.3,
  "warn_pct": 70,
  "blocked": false
}
```

**curl**
```bash
curl -s http://localhost:8088/budget \
  -H "Authorization: Bearer $HIVE_SECRET" | jq .
```

---

## Observability

### `GET /telemetry`

Model usage statistics since the process started.

**Response**
```json
{
  "inference_calls": 127,
  "tool_calls": 89,
  "input_tokens": 45230,
  "output_tokens": 12100,
  "cost_usd": 0.023,
  "cost_by_model": {
    "MiniMax-M3": 0.019,
    "MiniMax-M2.7": 0.004
  }
}
```

**curl**
```bash
curl -s http://localhost:8088/telemetry \
  -H "Authorization: Bearer $HIVE_SECRET" | jq .
```

---

### `GET /traces/{session_id}`

Per-session event trace. Pass `default` for the main session or any session ID from `/chat`.

**Parameters**

| Parameter | Location | Notes |
|---|---|---|
| `session_id` | path | Session ID to fetch trace for |

**Response**
```json
{
  "session_id": "default",
  "events": [
    {"type": "agent_turn_start", "ts": 1718123456.7, "data": {"session": "default"}},
    {"type": "inference_end", "ts": 1718123457.1, "data": {"model": "MiniMax-M3", "input_tokens": 350}}
  ],
  "sessions": ["default", "telegram:12345678"]
}
```

All events are JSON-serialisable. `sessions` lists all sessions with recorded events.

**curl**
```bash
curl -s http://localhost:8088/traces/default \
  -H "Authorization: Bearer $HIVE_SECRET" | jq '.events | length'
```

---

### `GET /audit`

Recent tool-call audit entries (secret values are redacted before storage).

**Query parameters**

| Parameter | Default | Notes |
|---|---|---|
| `limit` | `50` | Number of entries to return (max 200) |

**Response**
```json
{
  "entries": [
    {
      "id": 1,
      "ts": 1718123456.7,
      "tool": "web_get",
      "args": {"url": "https://..."},
      "status": "ok",
      "approved": false,
      "duration_ms": 312
    }
  ]
}
```

---

### `GET /tasks`

Task board state — the durable SQLite queue that drives the autonomy loop.

**Response**
```json
{
  "pending": 2,
  "tasks": [
    {
      "id": "a1b2c3d4",
      "kind": "tool",
      "state": "pending",
      "source": "cron",
      "attempts": 0,
      "last_error": null,
      "created_ts": 1718120000.0
    }
  ]
}
```

Returns at most the 20 most recent tasks (all states, newest first).

**curl**
```bash
curl -s "http://localhost:8088/audit?limit=20" \
  -H "Authorization: Bearer $HIVE_SECRET" | jq '.entries[] | .tool'

curl -s http://localhost:8088/tasks \
  -H "Authorization: Bearer $HIVE_SECRET" | jq '{pending, count: (.tasks | length)}'
```

---

## Approvals

Dangerous tool calls and REVIEW-tier self-mod edits are held in the approval gate.
The dashboard polls `/approvals` and lets you approve/deny each one.

### `GET /approvals`

List pending approvals.

**Response**
```json
{
  "pending": [
    {
      "approval_id": "abc123",
      "tool": "deploy",
      "args": {"target": "gateway"},
      "reason": "restart hiveos-gateway.service",
      "kind": "dangerous_tool"
    }
  ]
}
```

---

### `POST /approvals/decide`

Approve or deny a pending item.

**Request**
```json
{
  "approval_id": "abc123",
  "approved": true
}
```

**Response — approved dangerous tool**
```json
{
  "executed": true,
  "status": "ok",
  "result": "hiveos-gateway: ok\n",
  "error": null
}
```

**Response — approved self-mod edit**
```json
{
  "executed": true,
  "status": "applied",
  "branch": "hive/auto-1718123456",
  "detail": "pushed and PR opened"
}
```

**Response — denied**
```json
{
  "executed": false
}
```

**Errors:**
- `404` — `approval_id` not found or already resolved
- `{"executed": false, "error": "edit not found (process may have restarted)"}` — REVIEW-tier edit was approved but the process restarted; the edit is lost and must be re-triggered

**curl**
```bash
# list pending approvals
curl -s http://localhost:8088/approvals \
  -H "Authorization: Bearer $HIVE_SECRET" | jq '.pending[].approval_id'

# approve an item
curl -s -X POST http://localhost:8088/approvals/decide \
  -H "Authorization: Bearer $HIVE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"approval_id": "abc123", "approved": true}' | jq .

# deny an item
curl -s -X POST http://localhost:8088/approvals/decide \
  -H "Authorization: Bearer $HIVE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"approval_id": "abc123", "approved": false}' | jq .
```

---

## Telegram

### `POST /telegram/webhook`

Receives Telegram Bot API updates. Called by Telegram, not by clients directly.
Hive replies to an allowlisted user and the conversation is stored under an isolated session per chat, user and optional forum topic.

The route is registered only when `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `TELEGRAM_ALLOWED_USER_IDS` are all configured. Authentication is via the `X-Telegram-Bot-Api-Secret-Token` header. Requests larger than 1 MiB, malformed updates, and unauthorized users/chats never reach `hive.ask()`.

**Request** — raw Telegram `Update` object (JSON)

**Response**
```json
{"ok": true, "handled": true}
```

---

## Mission Control dashboard

### `GET /app/*`

Serves the Vite-built React SPA from `dashboard/dist/`. Only mounted if
`dashboard/dist/` exists (build it with `cd dashboard && npm ci && npm run build`).

The SPA polls:
- `/telemetry` every 10 s — model usage panel
- `/audit?limit=20` every 6 s — recent executions panel
- `/tasks` every 5 s — task queue panel
- `/chat/stream` — chat pane (SSE token streaming)
- `/approvals` — approval inbox

---

## Error handling

All authenticated endpoints return:

| HTTP status | Meaning |
|---|---|
| `200` | Success |
| `401` | Missing or invalid `Authorization: Bearer` header |
| `404` | Resource not found (e.g. unknown `approval_id`) |
| `422` | Pydantic validation error (malformed request body) |
| `500` | Unhandled server error (check `journalctl -u hiveos-gateway`) |

The `/chat/stream` SSE endpoint never returns a non-200 status for model errors — errors
arrive as `event: error` SSE events so the stream stays open and the client can reconnect.

---

## Protocol versioning

`ChatResponse` and `GET /health` carry `protocol_version`. The version follows
**additive-first** semantics: new response fields are added without bumping the version;
the version bumps only on breaking removal or rename. Current: `"1.0"`.

---

## Extended API (added in P21–P25)

All extended endpoints require `Authorization: Bearer <HIVE_SECRET>` unless noted.

---

### `GET /health/full`

Full system health snapshot from `HiveOS.health()`.

```json
{"status": "ok", "budget": {...}, "tasks": {...}, "memory": {...}, "telemetry": {...}}
```

---

### `GET /health/summary`

Concise health snapshot highlighting actionable concerns.

```json
{
  "budget": {"warning": null, "calls_today": 12, "remaining_calls": 2988, "calls_per_hour": 3.0},
  "tasks": {"pending": 1, "running": 0, "failed": 0, "pending_by_kind": {}, "avg_age_pending_secs": 4.2},
  "cron": {"total": 2, "enabled": 2, "overdue": 0},
  "self_mod": {"proposals": 3, "success_rate": 0.667, "recent_branches": ["hive/auto-1718123456"]},
  "audit": {"error_rate_24h": 0.02}
}
```

---

## Budget (extended)

### `GET /budget/detail`

Snapshot with `remaining_calls` and `is_near_cap`.

### `GET /budget/forecast?days=7`

Linear projection of budget spend (SPRINT_7 Batch F). Query param `days`
(default 7, clamped to 1-365) sets the projection horizon.

Returns `projected_total`, `daily_avg`, `max_daily` (USD), `days_until_cap`
(int or null), `status` (`ok` / `warn` / `critical` / `exceeded`), and
`confidence` (0-1, based on day-to-day spend variance). Empty history
returns safe defaults (`status="ok"`, `days_until_cap=null`, `confidence=0`).

### `GET /budget/warning`

Returns `{"warning": {...}}` when near cap or credit limit; `{"warning": null}` when healthy.

---

## System

### `GET /system-status`

Full system status snapshot: router config, budget forecast, memory, tasks, tools. Calls `HiveOS.system_status()`.

---

## Config

### `GET /config/validate`

```json
{"valid": true, "issues": []}
```

### `GET /config/summary`

Full config dict with all secrets replaced by `"***"`.

### `GET /config/llm`

Model configuration only (no secrets):

```json
{"exec_provider": "minimax", "exec_model": "MiniMax-M3", "exec_fallback_model": "...", "aux_model": "...", "planner_enabled": false, "daily_call_cap": 3000, "max_iterations": 30, "max_per_tool": 50}
```

---

## Tools (extended)

### `GET /tools`

All registered tools with name, category, description, dangerous flag, and availability.

### `GET /tools/dangerous`

```json
{"tools": ["deploy", "spend_money", "external_message"], "count": 3}
```

### `GET /tools/categories`

```json
{"categories": ["file", "web", "shell", "system", "memory"], "count": 5}
```

### `GET /tools/stats`

Aggregate stats from `ToolExecutor.stats()` — call counts, error counts, etc.

---

## Memory (extended)

### `GET /memory/stats`

```json
{"knowledge_count": 42, "episodic_count": 310, "avg_importance": 0.71, "oldest_ts": 1718000000.0, "newest_ts": 1718123456.0, "by_kind": {"fact": 30, "decision": 12}}
```

### `GET /memory/important?limit=10`

Top-N knowledge rows by importance score.

```json
{"facts": [{"id": 1, "topic": "...", "content": "...", "importance": 0.95, ...}], "count": 10}
```

### `GET /memory/topics?kind=<kind>`

All knowledge topics, optionally filtered by kind.

### `GET /memory/export`

Full backup of all knowledge and episodic entries.

### `POST /memory/{session_id}/consolidate`

Runs memory-keeper consolidation for the session. Returns `new_items` count.

### `GET /memory/session/{session_id}/count`

Episodic turn count for a session.

### `DELETE /memory/session/{session_id}`

Delete all episodic memory for a session.

### `DELETE /memory/wipe-knowledge?kind=<kind>`

Delete knowledge entries, optionally filtered by kind.

---

## Traces (extended)

### `GET /traces`

List of all session IDs with recorded events.

### `GET /traces/stats`

```json
{"session_count": 3, "total_events": 142, "sessions": ["default", "ws", "telegram:12345"]}
```

### `DELETE /traces/{session_id}`

Clear all events for a session. Returns `{"cleared": N}`.

### `GET /traces/export/{session_id}`

JSON-serialisable event list for the session.

---

## Audit (extended)

### `GET /audit/stats`

Audit summary grouped by tool and status.

### `GET /audit/search?tool=<name>&status=<ok|error>&limit=50`

Filter audit log by tool name and/or status.

### `GET /audit/error-rate?window_hours=24.0`

```json
{"error_rate": 0.04, "window_hours": 24.0}
```

### `GET /audit/errors?limit=20`

Most recent non-OK audit entries.

### `GET /audit/recent/{tool}?limit=20`

Most recent audit entries for a specific tool name.

### `DELETE /audit/purge?max_age_days=90.0`

Delete entries older than `max_age_days`. Returns `{"deleted": N}`.

### `GET /audit/export?start_ts=<ts>&end_ts=<ts>`

Export audit entries for a time range (omit params for all).

---

## Tasks (extended)

### `GET /tasks?kind=<k>&source=<s>&state=<st>`

Supports optional query params for filtering. Without params returns last 20 tasks.

### `GET /tasks/by-kind`

```json
{"by_kind": {"tool": 4, "commitment": 1, "self_improve": 2}}
```

### `GET /tasks/stats`

`TaskBoard.statistics()` — counts by state plus avg attempts.

### `GET /tasks/failed?limit=10`

Most recently failed tasks.

### `GET /tasks/last-failed`

Single most recently failed task or `{"task": null}`.

### `GET /tasks/running`

All currently RUNNING tasks.

### `POST /tasks/retry-failed`

Bulk-reset all FAILED tasks to PENDING.

### `POST /tasks/bulk-cancel`

Cancel all PENDING tasks. Body: `{"kind": "tool"}` to filter by kind.

### `POST /tasks/requeue-running`

Recover only RUNNING tasks whose explicit worker lease has expired. Active leases and legacy unleased rows remain untouched to avoid replaying an unknown side effect.

### `GET /tasks/{task_id}`

Single task by integer ID. `404` if not found.

### `POST /tasks/{task_id}/retry`

Reset a FAILED task to PENDING. `409` if not in failed state.

### `POST /tasks/{task_id}/cancel`

Cancel a PENDING task. `409` if not in pending state.

---

## Sessions

### `GET /sessions`

List all session IDs.

### `GET /sessions/stats`

Aggregate session statistics from `SessionStore.stats()`.

### `GET /sessions/search?q=<query>&session_id=<id>&limit=10`

Full-text search across session messages.

### `GET /sessions/{session_id}`

```json
{"session_id": "default", "message_count": 42, "title": "Task planning session", "summary": null}
```

### `GET /sessions/{session_id}/title`

Session title only.

### `POST /sessions/{session_id}/title`

Set title manually. Body: `{"title": "My session"}`.

### `POST /sessions/{session_id}/auto-title`

Generate a title from the first message using the aux model.

### `DELETE /sessions/{session_id}`

Delete the session and all its messages. Returns `{"deleted": true}`.

---

## Cron

### `GET /cron/stats`

```json
{"total": 3, "enabled": 2, "due_now": 0}
```

### `GET /cron`

All registered cron jobs with schedule, task_kind, payload, enabled, last_run, next_run.

### `POST /cron`

Add a new cron job. Body: `{"schedule": "0 3 * * *", "task_kind": "consolidate", "payload": {}, "enabled": true}`.

### `GET /cron/{job_id}`

Single cron job by ID.

### `POST /cron/{job_id}/enable` / `POST /cron/{job_id}/disable`

Toggle a cron job's enabled state.

### `DELETE /cron/{job_id}`

Remove a cron job. `404` if not found.

---

## Commitments

### `GET /commitments?active_only=false`

All commitment records.

### `POST /commitments`

Add a new commitment. Body: `{"description": "daily digest", "cadence_seconds": 86400, "task_kind": "commitment", "payload": {}}`.

### `DELETE /commitments/{commitment_id}`

Remove a commitment.

### `POST /commitments/{commitment_id}/fulfill`

Mark a commitment as fulfilled (updates `last_fulfilled`).

### `GET /commitments/overdue`

Active commitments that are currently overdue.

### `GET /commitments/upcoming?limit=5`

Next N active commitments sorted by next-due time (soonest first).

```json
{"upcoming": [{"id": 1, "description": "...", "cadence_seconds": 3600, "last_fulfilled": 1718120000.0}], "count": 1}
```

### `GET /commitments/active`

Descriptions of all active commitments.

---

## Approvals (extended)

### `GET /approvals/edits`

```json
{"pending_edits": [{"approval_id": "abc", "op": "patch_code", "file": "..."}], "count": 1}
```

### `POST /approvals/cancel`

Cancel a pending REVIEW-tier edit without applying it. Body: `{"approval_id": "abc123"}`.

### `DELETE /approvals/cancel-all`

Cancel all pending REVIEW-tier self-mod edits at once. Returns `{"cancelled": N}`.

---

## Skills

### `GET /skills`

Aggregate skill usage stats.

### `GET /skills/unused`

Active skills with `use_count == 0`.

### `GET /skills/archived`

All archived skills.

### `GET /skills/recent?limit=10`

Skills ordered by most recently used.

### `GET /skills/{name}`

Detail for a single skill.

### `POST /skills/{name}/pin`

Pin a skill (prevents archiving by Curator).

### `POST /skills/{name}/unpin`

Remove the pin from a skill.

---

## LLM

### `GET /llm/pool`

Credential pool status.

```json
{"pool_size": 2, "available": 2, "labels": ["key-xxxx", "key-yyyy"], "failure_counts": {"key-xxxx": 0, "key-yyyy": 0}, "total_failures": 0}
```

### `GET /model/catalog`

All registered model IDs from the `ModelCatalog`.

---

## Self-improvement

### `GET /self-improve/status`

Comprehensive status: pending review count, pending edit descriptions, recent branches, last result, history count.

### `GET /self-improve/pending`

Detailed metadata for all pending REVIEW-tier edits.

### `GET /self-improve/stages`

```json
{"by_stage": {"pushed": 3, "test_failed": 1, "protected": 0}, "total": 4}
```

### `GET /self-improve/history?limit=20`

Most recent self-mod proposal outcomes (newest first).

### `POST /self-improve/symptom`

Trigger a symptom-based improvement cycle. Body: `{"symptom": "tool executor is returning 503 on every call"}`.

```json
{"outcomes": [{"status": "pushed", "op": "patch_code", "tier": "AUTO", "detail": "...", "branch": "hive/auto-...", "approval_id": null}]}
```

### `POST /self-diagnose?dry_run=false`

Run the test suite and trigger improvement for any failures. Use `dry_run=true` for a no-op smoke check.

### `POST /run-tests?dry_run=false`

Run the project test suite without triggering any self-modification.

---

## Events

### `GET /events/history?n=20`

```json
{"events": [{"event_type": "inference_end", "ts": 1718123456.7, "data": {...}}], "count": 20}
```

### `GET /events/stats`

```json
{"by_type": {"inference_end": 127, "tool_call_end": 89}, "total": 216, "subscribers": 4}
```

---

## Loop Guard

### `GET /loop-guard/stats`

Current loop-guard statistics from `HiveOS.loop_guard_stats()`.

### `POST /loop-guard/reset`

Reset the loop-guard call history and per-tool counters.

### `GET /loop-guard/top-tools?n=5`

```json
{"tools": [{"name": "web_get", "calls": 12}, {"name": "read_file", "calls": 8}]}
```
