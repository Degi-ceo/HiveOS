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
Hive replies to the user and the conversation is stored under session `telegram:<chat_id>`.

Authentication is via the `X-Telegram-Bot-Api-Secret-Token` header (set when registering
the webhook with Telegram). Leave `TELEGRAM_WEBHOOK_SECRET` empty to skip verification.

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
