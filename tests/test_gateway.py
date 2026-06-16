"""P8 — gateway: /health /chat /ws /budget /approvals over a built HiveOS."""
from __future__ import annotations

from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.core.types import ToolCall
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="m")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="m")

    async def aclose(self):
        pass


def _hive(tmp_path, script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter(script or []))


def _client(hive) -> TestClient:
    return TestClient(create_app(hive))


_TOKEN = {"X-Hive-Token": "change_me"}  # default HIVE_SECRET


def test_health_is_open(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"


def test_chat_requires_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_returns_reply(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="hello back", model="m")])
    with _client(hive) as c:
        r = c.post("/chat", json={"message": "hi", "session_id": "s1"}, headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["reply"] == "hello back" and body["session_id"] == "s1"
        assert body["protocol_version"]  # B4: responses carry the protocol version


def test_budget_snapshot(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget", headers=_TOKEN)
        assert r.status_code == 200 and r.json()["daily_cap"] == 3000


def test_approvals_flow_gated_then_executed(tmp_path):
    # model asks for a dangerous tool -> it is gated (pending), not executed
    call = ToolCall(id="c1", name="deploy", arguments='{"target": "prod"}')
    hive = _hive(tmp_path, [CompletionResult(text="", model="m", tool_calls=[call]),
                            CompletionResult(text="queued", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "ship it"}, headers=_TOKEN)
        pending = c.get("/approvals", headers=_TOKEN).json()["pending"]
        assert pending and pending[0]["tool"] == "deploy"
        aid = pending[0]["id"]
        # approve -> the gated tool now runs
        r = c.post("/approvals/decide", json={"approval_id": aid, "approved": True}, headers=_TOKEN)
        assert r.status_code == 200 and r.json()["executed"] is True
        assert "prod" in r.json()["result"]
        # unknown id -> 404
        assert c.post("/approvals/decide", json={"approval_id": "zzz", "approved": True},
                      headers=_TOKEN).status_code == 404


def test_ws_handshake_and_reply(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="ws reply", model="m")])
    with _client(hive) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("change_me")        # token handshake
            ws.send_text("hello over ws")
            assert ws.receive_json() == {"type": "reply", "data": "ws reply"}


def test_ws_rejects_bad_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("wrong")
            assert ws.receive_json()["data"] == "unauthorized"


def test_chat_hides_exception_detail(tmp_path):
    class _BoomRouter(_ScriptRouter):
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            raise RuntimeError("secret db password in stacktrace")

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_BoomRouter([]))
    with _client(hive) as c:
        r = c.post("/chat", json={"message": "hi"}, headers=_TOKEN)
        assert r.status_code == 503
        body = r.json()
        assert "secret db password" not in str(body)
        assert "RuntimeError" not in str(body)


def test_ws_error_sends_generic_message(tmp_path):
    class _BoomRouter(_ScriptRouter):
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            raise RuntimeError("secret ws stacktrace")

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_BoomRouter([]))
    with _client(hive) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("change_me")
            ws.send_text("trigger error")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "secret ws stacktrace" not in str(msg)
            assert "RuntimeError" not in str(msg)
            assert msg["data"] == "internal error"


# ---------------------------------------------------------------------------
# gateway/auth.py — token_ok() and make_auth_dependency() unit tests
# ---------------------------------------------------------------------------

def test_token_ok_matching():
    from hive.gateway.auth import token_ok
    assert token_ok("abc123", "abc123") is True


def test_token_ok_mismatch():
    from hive.gateway.auth import token_ok
    assert token_ok("wrong", "abc123") is False


def test_token_ok_none_token():
    from hive.gateway.auth import token_ok
    assert token_ok(None, "abc123") is False


def test_token_ok_empty_token():
    from hive.gateway.auth import token_ok
    assert token_ok("", "abc123") is False


def test_make_auth_dependency_blocks_missing_header(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget").status_code == 401


def test_make_auth_dependency_blocks_wrong_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget", headers={"X-Hive-Token": "nope"}).status_code == 401


def test_make_auth_dependency_allows_correct_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget", headers=_TOKEN).status_code == 200


# ---------------------------------------------------------------------------
# /approvals/decide — self_mod routing
# ---------------------------------------------------------------------------

def test_approvals_decide_self_mod_routes_to_improver(tmp_path):
    """REVIEW-tier self-mod approvals must go through improver.apply_approved,
    not the tool executor (which would return 'unknown tool: self_mod:...')."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier

    hive = _hive(tmp_path)

    async def _noop_apply(wt):
        return []

    edit = Edit(op=EditOp.PATCH_CODE, summary="fix crash",
                rationale="it explodes", apply=_noop_apply,
                risk_tier=RiskTier.REVIEW)
    approval_id = gate.request("self_mod:patch_code", {"summary": "fix crash"},
                               "test reason")
    hive.edit_pending[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": True},
                   headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True
    # SelfModifier will fail (no real git repo), but it must NOT say "unknown tool"
    assert "unknown tool" not in str(body)


def test_approvals_decide_self_mod_rejection_cleans_edit_pending(tmp_path):
    """Rejecting a self_mod approval must remove it from edit_pending."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier

    hive = _hive(tmp_path)

    async def _noop_apply(wt):
        return []

    edit = Edit(op=EditOp.PATCH_CODE, summary="fix crash",
                rationale="it explodes", apply=_noop_apply,
                risk_tier=RiskTier.REVIEW)
    approval_id = gate.request("self_mod:patch_code", {"summary": "fix crash"},
                               "test reason")
    hive.edit_pending[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["executed"] is False
    assert approval_id not in hive.edit_pending


def test_approvals_decide_self_mod_missing_edit_returns_error(tmp_path):
    """If the edit was lost (process restart), the decide endpoint should return
    an error rather than crashing."""
    from hive.core.approval import gate

    hive = _hive(tmp_path)
    approval_id = gate.request("self_mod:patch_code", {"summary": "gone"},
                               "test")
    # do NOT store anything in edit_pending

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": True},
                   headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False
    assert "error" in body


def test_approvals_cancel_removes_pending_edit(tmp_path):
    """POST /approvals/cancel must remove the REVIEW-tier edit from the pending store."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier
    hive = _hive(tmp_path)
    approval_id = gate.request("self_mod:patch_code", {"summary": "x"}, "rationale")

    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="x", apply=_noop)
    edit.risk_tier = RiskTier.REVIEW
    hive.edit_pending[approval_id] = edit
    hive.improver._pending_store[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/cancel",
                   json={"approval_id": approval_id, "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert approval_id not in hive.improver._pending_store


def test_approvals_cancel_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/approvals/cancel",
                   json={"approval_id": "nonexistent", "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 404


def test_approvals_includes_pending_edits_count(tmp_path):
    """GET /approvals must include pending_edits count from the improver."""
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/approvals", headers=_TOKEN).json()
    assert "pending_edits" in body
    assert isinstance(body["pending_edits"], int)


# ---------------------------------------------------------------------------
# /sessions — list, search, delete
# ---------------------------------------------------------------------------

def test_sessions_list_returns_session_ids(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="hi", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "hello", "session_id": "sess1"}, headers=_TOKEN)
        body = c.get("/sessions", headers=_TOKEN).json()
    assert "sessions" in body
    assert "sess1" in body["sessions"]


def test_sessions_search_finds_message(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="pong", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "findme_token", "session_id": "s1"},
               headers=_TOKEN)
        body = c.get("/sessions/search", params={"q": "findme_token"},
                     headers=_TOKEN).json()
    assert body["count"] >= 1
    assert any("findme_token" in r["content"] for r in body["results"])


def test_sessions_delete_removes_messages(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="bye", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "delete me", "session_id": "del1"},
               headers=_TOKEN)
        r = c.delete("/sessions/del1", headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1


# ---------------------------------------------------------------------------
# /cron — list, add, enable/disable, delete
# ---------------------------------------------------------------------------

def test_cron_add_and_list(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@hourly", "task_kind": "ping"},
                   headers=_TOKEN)
        assert r.status_code == 200
        job_id = r.json()["id"]
        jobs = c.get("/cron", headers=_TOKEN).json()["jobs"]
    assert any(j["id"] == job_id for j in jobs)


def test_cron_disable_enable(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@daily", "task_kind": "noop"},
                   headers=_TOKEN)
        jid = r.json()["id"]
        r2 = c.post(f"/cron/{jid}/disable", headers=_TOKEN)
        assert r2.json()["enabled"] is False
        r3 = c.post(f"/cron/{jid}/enable", headers=_TOKEN)
        assert r3.json()["enabled"] is True


def test_cron_delete_removes_job(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@weekly", "task_kind": "x"},
                   headers=_TOKEN)
        jid = r.json()["id"]
        r2 = c.delete(f"/cron/{jid}", headers=_TOKEN)
        assert r2.json()["removed"] is True
        r3 = c.delete(f"/cron/{jid}", headers=_TOKEN)
        assert r3.status_code == 404


def test_cron_add_missing_fields_returns_422(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@hourly"}, headers=_TOKEN)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /tasks/stats and /tasks/retry-failed and /tasks?state=
# ---------------------------------------------------------------------------

def test_tasks_stats_returns_totals(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("ping", {}, source="test")
    with _client(hive) as c:
        body = c.get("/tasks/stats", headers=_TOKEN).json()
    assert "total" in body and "by_state" in body


def test_tasks_retry_failed_resets_failed(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("job", {})
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "boom")
    with _client(hive) as c:
        body = c.post("/tasks/retry-failed", headers=_TOKEN).json()
    assert body["retried"] == 1


def test_tasks_filter_by_kind(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("alpha", {})
    hive.task_board.enqueue("beta", {})
    with _client(hive) as c:
        body = c.get("/tasks", params={"kind": "alpha"}, headers=_TOKEN).json()
    assert all(t["kind"] == "alpha" for t in body["tasks"])
    assert len(body["tasks"]) == 1


# ---------------------------------------------------------------------------
# /audit/export
# ---------------------------------------------------------------------------

def test_audit_export_returns_entries(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "ping", "status": "ok", "approved": True})
    with _client(hive) as c:
        body = c.get("/audit/export", headers=_TOKEN).json()
    assert body["count"] >= 1
    assert all("tool" in e for e in body["entries"])


# ---------------------------------------------------------------------------
# /health/full — full system health snapshot
# ---------------------------------------------------------------------------

def test_health_full_requires_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/health/full").status_code == 401


def test_health_full_returns_snapshot(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/health/full", headers=_TOKEN).json()
    assert body["status"] == "ok"
    assert "budget" in body
    assert "tasks" in body
    assert "telemetry" in body
    assert isinstance(body["tools"], int)
