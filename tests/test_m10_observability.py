"""
test_m10_observability.py — M10-a: telemetry/traces/audit/tasks gateway endpoints.

All tests are offline (mock router, in-memory DB). Verifies shape, auth, and
that the endpoints read directly from the wired runtime objects.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS

# Default secret from HiveConfig.from_env with no overrides.
_TOKEN = {"X-Hive-Token": "change_me"}


class _ScriptRouter:
    """Fake router — no network."""
    def __init__(self, replies=None):
        self._replies = list(replies or [])
        self._idx = 0

    async def complete(self, messages, *, system="", tools=None, **kw):
        if self._replies and self._idx < len(self._replies):
            r = self._replies[self._idx]; self._idx += 1; return r
        return CompletionResult(text="ok", tool_calls=[], model="test",
                                input_tokens=1, output_tokens=1, cost_usd=0.0)

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# /telemetry
# ---------------------------------------------------------------------------

def test_telemetry_requires_auth(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/telemetry").status_code == 401


def test_telemetry_shape(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        r = c.get("/telemetry", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    for key in ("inference_calls", "input_tokens", "output_tokens",
                "tool_calls", "cost_usd", "by_model", "cost_by_model"):
        assert key in body, f"missing key: {key}"


def test_telemetry_initial_zeros(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        body = c.get("/telemetry", headers=_TOKEN).json()
    assert body["inference_calls"] == 0
    assert body["tool_calls"] == 0
    assert body["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# /traces/{session_id}
# ---------------------------------------------------------------------------

def test_traces_requires_auth(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/traces/default").status_code == 401


def test_traces_shape(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        r = c.get("/traces/default", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert "session_id" in body
    assert "events" in body
    assert "sessions" in body
    assert isinstance(body["events"], list)
    assert isinstance(body["sessions"], list)


def test_traces_unknown_session_returns_empty(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        body = c.get("/traces/no-such-session", headers=_TOKEN).json()
    assert body["events"] == []


# ---------------------------------------------------------------------------
# /audit
# ---------------------------------------------------------------------------

def test_audit_requires_auth(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/audit").status_code == 401


def test_audit_shape(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        r = c.get("/audit", headers=_TOKEN)
    assert r.status_code == 200
    assert "entries" in r.json()
    assert isinstance(r.json()["entries"], list)


def test_audit_initial_empty(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/audit", headers=_TOKEN).json()["entries"] == []


def test_audit_limit_param(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(5):
        hive.audit_log.record({"tool": f"tool_{i}", "status": "ok",
                                "approved": True, "error": None, "args": {}})
    with TestClient(create_app(hive)) as c:
        body = c.get("/audit?limit=3", headers=_TOKEN).json()
    assert len(body["entries"]) <= 3


def test_audit_entries_have_expected_keys(tmp_path):
    hive = _make_hive(tmp_path)
    hive.audit_log.record({"tool": "read_file", "status": "ok",
                            "approved": True, "error": None, "args": {"path": "/tmp/x"}})
    with TestClient(create_app(hive)) as c:
        entries = c.get("/audit", headers=_TOKEN).json()["entries"]
    assert entries
    e = entries[0]
    for key in ("tool", "status", "approved", "error"):
        assert key in e, f"missing key: {key}"


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

def test_tasks_requires_auth(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/tasks").status_code == 401


def test_tasks_shape(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        r = c.get("/tasks", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert "pending" in body and isinstance(body["pending"], int)
    assert "tasks" in body and isinstance(body["tasks"], list)


def test_tasks_initially_empty(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        body = c.get("/tasks", headers=_TOKEN).json()
    assert body["pending"] == 0
    assert body["tasks"] == []


def test_tasks_reflects_enqueued(tmp_path):
    hive = _make_hive(tmp_path)
    hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
    with TestClient(create_app(hive)) as c:
        body = c.get("/tasks", headers=_TOKEN).json()
    assert body["pending"] >= 1
    assert any(t["kind"] == "tool" for t in body["tasks"])


def test_tasks_entry_shape(tmp_path):
    hive = _make_hive(tmp_path)
    hive.task_board.enqueue("tool", {"note": "hello"}, source="pytest")
    with TestClient(create_app(hive)) as c:
        tasks = c.get("/tasks", headers=_TOKEN).json()["tasks"]
    assert tasks
    for key in ("id", "kind", "state", "source", "attempts", "created_ts", "payload"):
        assert key in tasks[0], f"missing key: {key}"


# ---------------------------------------------------------------------------
# /tasks/{id}, /tasks/{id}/retry, /tasks/{id}/cancel
# ---------------------------------------------------------------------------

def test_task_get_by_id(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {"tool": "read_file"}, source="test")
    with TestClient(create_app(hive)) as c:
        r = c.get(f"/tasks/{tid}", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == tid and body["kind"] == "tool"


def test_task_get_unknown_returns_404(tmp_path):
    with TestClient(create_app(_make_hive(tmp_path))) as c:
        assert c.get("/tasks/99999", headers=_TOKEN).status_code == 404


def test_task_retry_resets_failed(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("job", {}, source="test")
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "error")
    with TestClient(create_app(hive)) as c:
        r = c.post(f"/tasks/{tid}/retry", headers=_TOKEN)
        assert r.status_code == 200
        assert r.json()["retried"] is True
        # Check state via GET endpoint (keeps DB open within client context)
        task = c.get(f"/tasks/{tid}", headers=_TOKEN).json()
        assert task["state"] == "pending"


def test_task_retry_non_failed_returns_409(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("job", {}, source="test")  # pending
    with TestClient(create_app(hive)) as c:
        assert c.post(f"/tasks/{tid}/retry", headers=_TOKEN).status_code == 409


def test_task_cancel_pending(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("job", {}, source="test")
    with TestClient(create_app(hive)) as c:
        r = c.post(f"/tasks/{tid}/cancel", headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["cancelled"] is True


def test_task_cancel_non_pending_returns_409(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("job", {}, source="test")
    hive.task_board.claim(tid)
    hive.task_board.complete(tid)
    with TestClient(create_app(hive)) as c:
        assert c.post(f"/tasks/{tid}/cancel", headers=_TOKEN).status_code == 409


# --- trace utilities -----------------------------------------------------------

def test_trace_collector_clear_session():
    from hive.core.events import EventBus, EventType
    from hive.observability.traces import TraceCollector
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"session": "s1"})
    bus.publish(EventType.INFERENCE_END, {"session": "s1"})
    assert tc.event_count("s1") == 2
    cleared = tc.clear("s1")
    assert cleared == 2
    assert tc.event_count("s1") == 0


def test_trace_collector_clear_all():
    from hive.core.events import EventBus, EventType
    from hive.observability.traces import TraceCollector
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"session": "a"})
    bus.publish(EventType.INFERENCE_END, {"session": "b"})
    total = tc.clear()
    assert total == 2
    assert tc.sessions() == []


def test_trace_collector_event_count():
    from hive.core.events import EventBus, EventType
    from hive.observability.traces import TraceCollector
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    assert tc.event_count("x") == 0
    bus.publish(EventType.TOOL_CALL_END, {"session": "x"})
    assert tc.event_count("x") == 1


# --- AuditLog.purge_old() and count() -----------------------------------------

def test_audit_log_count(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    assert log.count() == 0
    log.record({"tool": "read_file", "status": "ok"})
    log.record({"tool": "write_file", "status": "ok"})
    assert log.count() == 2


def test_audit_log_purge_old(tmp_path):
    now = [0.0]
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite", clock=lambda: now[0])
    log.record({"tool": "old_tool", "status": "ok"})  # ts=0
    now[0] = 100 * 86_400                              # advance 100 days
    log.record({"tool": "new_tool", "status": "ok"})  # ts=100 days
    purged = log.purge_old(max_age_days=90)
    assert purged == 1
    remaining = log.recent(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["tool"] == "new_tool"


def test_audit_log_purge_old_keeps_all_when_nothing_old(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "t1", "status": "ok"})
    log.record({"tool": "t2", "status": "ok"})
    purged = log.purge_old(max_age_days=90)
    assert purged == 0
    assert log.count() == 2


def test_audit_log_recent_by_tool(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "write_file", "status": "ok"})
    log.record({"tool": "shell", "status": "error", "error": "boom"})
    entries = log.recent_by_tool("shell", limit=10)
    assert len(entries) == 2
    assert all(e["tool"] == "shell" for e in entries)


def test_audit_log_recent_by_tool_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    assert log.recent_by_tool("nonexistent") == []


def test_audit_log_recent_by_tool_limit(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for _ in range(5):
        log.record({"tool": "shell", "status": "ok"})
    entries = log.recent_by_tool("shell", limit=3)
    assert len(entries) == 3
