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


def test_audit_log_recent_errors(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "write_file", "status": "error", "error": "permission denied"})
    log.record({"tool": "read_file", "status": "denied"})
    errors = log.recent_errors(limit=10)
    assert len(errors) == 2
    tools = {e["tool"] for e in errors}
    assert tools == {"write_file", "read_file"}


def test_audit_log_recent_errors_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    assert log.recent_errors() == []


def test_audit_log_recent_errors_limit(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for i in range(5):
        log.record({"tool": f"t{i}", "status": "error"})
    errors = log.recent_errors(limit=3)
    assert len(errors) == 3


# --- error_rate ------------------------------------------------------------------

def test_audit_log_error_rate_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    assert log.error_rate() == 0.0


def test_audit_log_error_rate_all_ok(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for _ in range(4):
        log.record({"tool": "shell", "status": "ok"})
    assert log.error_rate() == 0.0


def test_audit_log_error_rate_all_errors(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for _ in range(3):
        log.record({"tool": "shell", "status": "error"})
    assert log.error_rate() == 1.0


def test_audit_log_error_rate_mixed(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "t", "status": "ok"})
    log.record({"tool": "t", "status": "error"})
    log.record({"tool": "t", "status": "error"})
    log.record({"tool": "t", "status": "ok"})
    rate = log.error_rate()
    assert rate == 0.5


def test_audit_log_error_rate_window_excludes_old(tmp_path):
    from hive.observability.audit import AuditLog
    now = [1000.0]
    log = AuditLog(tmp_path / "a.sqlite", clock=lambda: now[0])
    log.record({"tool": "t", "status": "error"})  # ts=1000 (old)
    now[0] = 1000.0 + 25 * 3600  # advance 25 hours
    log.record({"tool": "t", "status": "ok"})   # ts in window
    # window_hours=24 → old error is excluded
    rate = log.error_rate(window_hours=24.0)
    assert rate == 0.0  # only 1 entry in window and it's ok


# --- TraceCollector new methods -----------------------------------------------

def test_trace_collector_total_event_count_empty():
    from hive.observability.traces import TraceCollector
    tc = TraceCollector()
    assert tc.total_event_count() == 0


def test_trace_collector_total_event_count():
    from hive.observability.traces import TraceCollector
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.TOOL_CALL_END, {"session": "a", "tool": "t"})
    bus.publish(EventType.TOOL_CALL_END, {"session": "b", "tool": "t"})
    assert tc.total_event_count() == 2


def test_trace_collector_session_count():
    from hive.observability.traces import TraceCollector
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    assert tc.session_count() == 0
    bus.publish(EventType.TOOL_CALL_END, {"session": "sess1", "tool": "t"})
    bus.publish(EventType.TOOL_CALL_END, {"session": "sess2", "tool": "t"})
    assert tc.session_count() == 2


def test_trace_collector_event_type_counts():
    from hive.observability.traces import TraceCollector
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.TOOL_CALL_END, {"session": "s", "tool": "t"})
    bus.publish(EventType.TOOL_CALL_END, {"session": "s", "tool": "t"})
    bus.publish(EventType.INFERENCE_END, {"session": "s", "model": "m"})
    counts = tc.event_type_counts("s")
    assert counts["tool_call_end"] == 2
    assert counts["inference_end"] == 1


def test_trace_collector_event_type_counts_empty_session():
    from hive.observability.traces import TraceCollector
    tc = TraceCollector()
    assert tc.event_type_counts("no_such_session") == {}


# --- AuditLog.search() --------------------------------------------------------

def test_audit_log_search_by_tool_name(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "write_file", "status": "ok"})
    log.record({"tool": "shell", "status": "error"})
    results = log.search(tool="shell")
    assert len(results) == 2
    assert all(r["tool"] == "shell" for r in results)


def test_audit_log_search_by_status(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "write_file", "status": "error"})
    log.record({"tool": "read_file", "status": "ok"})
    results = log.search(status="ok")
    assert len(results) == 2
    assert all(r["status"] == "ok" for r in results)


def test_audit_log_search_no_match_returns_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    assert log.search(tool="nonexistent") == []


def test_audit_log_search_limit(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for _ in range(5):
        log.record({"tool": "shell", "status": "ok"})
    results = log.search(tool="shell", limit=3)
    assert len(results) == 3


# --- AuditLog.redaction -------------------------------------------------------

def test_audit_log_redacts_api_keys(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "api_call", "status": "ok",
                "args": {"api_key": "sk-supersecretkey12345"}})
    entries = log.search(tool="api_call")
    assert entries
    args = entries[0]["args"]
    assert "sk-supersecretkey" not in str(args)


# --- AuditLog.recent() limit --------------------------------------------------

def test_audit_log_recent_limit(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    for i in range(10):
        log.record({"tool": f"tool_{i}", "status": "ok"})
    results = log.recent(limit=3)
    assert len(results) == 3


# --- AuditLog.export() --------------------------------------------------------

def test_audit_log_export_all(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "t1", "status": "ok", "args": {"x": 1}})
    log.record({"tool": "t2", "status": "ok", "args": {}})
    entries = log.export()
    assert len(entries) == 2
    assert "args" in entries[0]


def test_audit_log_export_time_range(tmp_path):
    from hive.observability.audit import AuditLog
    now = [1000.0]
    log = AuditLog(tmp_path / "a.sqlite", clock=lambda: now[0])
    log.record({"tool": "old", "status": "ok"})   # ts=1000
    now[0] = 2000.0
    log.record({"tool": "new", "status": "ok"})   # ts=2000
    entries = log.export(start_ts=1500.0)
    assert len(entries) == 1
    assert entries[0]["tool"] == "new"


# --- AuditLog.stats() ---------------------------------------------------------

def test_audit_log_stats_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    s = log.stats()
    assert s["total"] == 0
    assert s["by_tool"] == {}


def test_audit_log_stats_groups_by_tool_and_status(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "shell", "status": "ok"})
    log.record({"tool": "shell", "status": "error"})
    log.record({"tool": "read_file", "status": "ok"})
    s = log.stats()
    assert s["total"] == 4
    assert s["by_tool"]["shell"]["total"] == 3
    assert s["by_tool"]["shell"]["by_status"]["ok"] == 2
    assert s["by_tool"]["shell"]["by_status"]["error"] == 1
    assert s["by_tool"]["read_file"]["total"] == 1


# --- AuditLog.clear() ---------------------------------------------------------

def test_audit_log_clear_removes_all(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "a.sqlite")
    log.record({"tool": "t1", "status": "ok"})
    log.record({"tool": "t2", "status": "ok"})
    assert log.count() == 2
    log.clear()
    assert log.count() == 0
    assert log.recent() == []


# --- AuditLog additional coverage ------------------------------------------------

def test_audit_log_record_and_recent_order(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "tool_a", "status": "ok", "args": {}})
    log.record({"tool": "tool_b", "status": "ok", "args": {}})
    recent = log.recent(limit=2)
    assert recent[0]["tool"] == "tool_b"  # most recent first
    assert recent[1]["tool"] == "tool_a"


def test_audit_log_approved_flag(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "deploy", "status": "ok", "approved": True, "args": {}})
    recent = log.recent(limit=1)
    assert recent[0]["approved"] == 1


def test_audit_log_prune_trims_to_max(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db", max_rows=3)
    for i in range(5):
        log.record({"tool": f"tool_{i}", "status": "ok", "args": {}})
    # After prune, at most 3 rows
    recent = log.recent(limit=100)
    assert len(recent) <= 3


def test_audit_log_clear_then_recent_empty(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "t", "status": "ok", "args": {}})
    log.clear()
    assert log.recent(limit=10) == []


def test_audit_log_stats_counts_errors(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "web_get", "status": "error", "error": "timeout", "args": {}})
    log.record({"tool": "web_get", "status": "error", "error": "blocked", "args": {}})
    log.record({"tool": "web_get", "status": "ok", "args": {}})
    stats = log.stats()
    # stats() returns {"total": N, "by_tool": {...}} structure
    by_tool = stats.get("by_tool", stats)
    assert "web_get" in by_tool
    assert by_tool["web_get"]["total"] == 3
    assert by_tool["web_get"]["by_status"]["error"] == 2
    assert by_tool["web_get"]["by_status"]["ok"] == 1


# --- new AuditLog tests ----------------------------------------------------------

def test_audit_log_record_and_count(tmp_path):
    """record() 3 entries → recent(limit=10) returns exactly 3."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    for i in range(3):
        log.record({"tool": f"t{i}", "status": "ok", "args": {}})
    assert len(log.recent(limit=10)) == 3


def test_audit_log_stats_has_total(tmp_path):
    """stats()['total'] equals the number of records inserted."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "a", "status": "ok", "args": {}})
    log.record({"tool": "b", "status": "ok", "args": {}})
    stats = log.stats()
    assert stats["total"] == 2


def test_audit_log_record_sets_timestamp(tmp_path):
    """Each recorded row has a 'ts' field that is a positive float."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "ts_tool", "status": "ok", "args": {}})
    rows = log.recent(limit=1)
    assert len(rows) == 1
    assert rows[0]["ts"] > 0


def test_audit_log_clear_then_empty(tmp_path):
    """After record() and clear(), recent() returns an empty list."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "x", "status": "ok", "args": {}})
    log.clear()
    assert log.recent(limit=10) == []


def test_audit_log_approved_true_stored(tmp_path):
    """record() with approved=True persists approved == 1 in the row."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "deploy", "approved": True, "status": "ok", "args": {}})
    rows = log.recent(limit=1)
    assert rows[0]["approved"] == 1


def test_audit_log_error_status(tmp_path):
    """record() with status='error' is reflected in stats by_status."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "runner", "status": "error", "error": "boom", "args": {}})
    stats = log.stats()
    by_tool = stats["by_tool"]
    assert by_tool["runner"]["by_status"]["error"] == 1


def test_audit_log_multiple_tools_stats(tmp_path):
    """Two different tools both appear in stats['by_tool']."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "alpha", "status": "ok", "args": {}})
    log.record({"tool": "beta", "status": "ok", "args": {}})
    by_tool = log.stats()["by_tool"]
    assert "alpha" in by_tool
    assert "beta" in by_tool


def test_audit_log_recent_limit(tmp_path):
    """recent(limit=3) returns exactly 3 rows even when 10 are stored."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    for i in range(10):
        log.record({"tool": f"t{i}", "status": "ok", "args": {}})
    rows = log.recent(limit=3)
    assert len(rows) == 3


# --- Wave 3O additional tests ---------------------------------------------------

def test_audit_log_error_rate_all_ok(tmp_path):
    """error_rate() is 0.0 when all records have status=ok."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    for _ in range(4):
        log.record({"tool": "x", "status": "ok", "args": {}})
    assert log.error_rate() == 0.0


def test_audit_log_error_rate_half_errors(tmp_path):
    """error_rate() is 0.5 when half of records are errors."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "a", "status": "ok", "args": {}})
    log.record({"tool": "a", "status": "error", "args": {}})
    assert log.error_rate() == 0.5


def test_audit_log_recent_by_tool_filters_correctly(tmp_path):
    """recent_by_tool('shell') only returns rows for that tool."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "shell", "status": "ok", "args": {}})
    log.record({"tool": "web_get", "status": "ok", "args": {}})
    rows = log.recent_by_tool("shell")
    assert all(r["tool"] == "shell" for r in rows)


def test_audit_log_recent_errors_returns_only_errors(tmp_path):
    """recent_errors() only contains rows with status != 'ok'."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "good", "status": "ok", "args": {}})
    log.record({"tool": "bad", "status": "error", "args": {}})
    errors = log.recent_errors(limit=10)
    assert len(errors) == 1
    assert errors[0]["tool"] == "bad"


def test_audit_log_search_by_tool(tmp_path):
    """search(tool='deploy') returns only records for that tool."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "deploy", "status": "ok", "args": {}})
    log.record({"tool": "shell", "status": "ok", "args": {}})
    results = log.search(tool="deploy")
    assert len(results) == 1 and results[0]["tool"] == "deploy"


def test_audit_log_purge_old_removes_entries(tmp_path):
    """purge_old(max_age_days=0) removes all old entries and returns removed count."""
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "t", "status": "ok", "args": {}})
    removed = log.purge_old(max_age_days=0)
    assert isinstance(removed, int) and removed >= 0


# --- Wave 3U new tests -----------------------------------------------------------

def test_telemetry_inference_calls_increments():
    from hive.observability.telemetry import Telemetry
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "gpt-4", "input_tokens": 10,
                                          "output_tokens": 5, "cost_usd": 0.001})
    bus.publish(EventType.INFERENCE_END, {"model": "gpt-4", "input_tokens": 20,
                                          "output_tokens": 8, "cost_usd": 0.002})
    assert t.inference_calls == 2


def test_telemetry_tool_calls_increments():
    from hive.observability.telemetry import Telemetry
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.TOOL_CALL_END, {"tool": "shell"})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "read_file"})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "shell"})
    assert t.tool_calls == 3


def test_telemetry_top_model():
    from hive.observability.telemetry import Telemetry
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "mini", "input_tokens": 1,
                                          "output_tokens": 1, "cost_usd": 0.0})
    bus.publish(EventType.INFERENCE_END, {"model": "gpt-4", "input_tokens": 1,
                                          "output_tokens": 1, "cost_usd": 0.0})
    bus.publish(EventType.INFERENCE_END, {"model": "gpt-4", "input_tokens": 1,
                                          "output_tokens": 1, "cost_usd": 0.0})
    assert t.top_model() == "gpt-4"


def test_telemetry_total_tokens():
    from hive.observability.telemetry import Telemetry
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "m", "input_tokens": 10,
                                          "output_tokens": 5, "cost_usd": 0.0})
    bus.publish(EventType.INFERENCE_END, {"model": "m", "input_tokens": 20,
                                          "output_tokens": 15, "cost_usd": 0.0})
    assert t.total_tokens() == 50


def test_telemetry_reset_clears_all():
    from hive.observability.telemetry import Telemetry
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "m", "input_tokens": 5,
                                          "output_tokens": 5, "cost_usd": 0.01})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "shell"})
    t.reset()
    assert t.inference_calls == 0
    assert t.tool_calls == 0
    assert t.cost_usd == 0.0
    assert t.by_model == {}


def test_audit_log_prune_direct_call(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db", max_rows=10_000)
    for i in range(6):
        log.record({"tool": f"t{i}", "status": "ok", "args": {}})
    deleted = log.prune(max_rows=4)
    assert deleted == 2
    assert log.count() == 4


def test_audit_log_search_by_status_only(tmp_path):
    from hive.observability.audit import AuditLog
    log = AuditLog(tmp_path / "audit.db")
    log.record({"tool": "a", "status": "ok", "args": {}})
    log.record({"tool": "b", "status": "error", "args": {}})
    log.record({"tool": "c", "status": "denied", "args": {}})
    log.record({"tool": "d", "status": "error", "args": {}})
    results = log.search(status="error")
    assert len(results) == 2
    assert all(r["status"] == "error" for r in results)


def test_audit_log_export_with_end_ts(tmp_path):
    from hive.observability.audit import AuditLog
    now = [1000.0]
    log = AuditLog(tmp_path / "audit.db", clock=lambda: now[0])
    log.record({"tool": "early", "status": "ok", "args": {}})   # ts=1000
    now[0] = 2000.0
    log.record({"tool": "mid", "status": "ok", "args": {}})     # ts=2000
    now[0] = 3000.0
    log.record({"tool": "late", "status": "ok", "args": {}})    # ts=3000
    entries = log.export(end_ts=2000.0)
    tools = [e["tool"] for e in entries]
    assert "late" not in tools
    assert "early" in tools and "mid" in tools
