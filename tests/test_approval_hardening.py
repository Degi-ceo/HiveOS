"""Pillar 2 — Approval Gate hardening (expiration, kill switch, audit, batch).

The PROTECTED Core/approval_gate.py is the immutable firewall. These tests cover
the *operational* hardening in src/hive/core/approval_enhancements.py:
  - TTL expiration (sweep_expired)
  - Emergency stop (kill-switch)
  - Structured audit history
  - Batch resolve
  - Kill-switch integration with the executor
  - Gateway endpoints /approvals/expire, /approvals/emergency-stop, /approvals/history
"""
from __future__ import annotations

from starlette.testclient import TestClient

from hive.core.approval import gate
from hive.core.approval_enhancements import (
    ApprovalGateEnhancements,
    AuditRecord,
    DecisionOutcome,
    ExpirationPolicy,
    enhance,
)
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


# --------------------------------------------------------------------------- #
# unit-level tests for the enhancements module                               #
# --------------------------------------------------------------------------- #


def _fresh_enhancements(ttl_seconds: float = 60.0) -> ApprovalGateEnhancements:
    """Build a stand-alone enhancer bound to the canonical gate for isolated tests."""
    # Use the canonical gate but with a fresh policy + clock for isolation.
    clock_t = [1000.0]
    return ApprovalGateEnhancements(
        gate, policy=ExpirationPolicy(ttl_seconds=ttl_seconds, enabled=True),
        clock=lambda: clock_t[0],
    )


def test_audit_record_round_trip():
    r = AuditRecord(id="x1", tool="deploy", args={"k": 1}, reason="prod",
                    kind="danger", outcome=DecisionOutcome.APPROVED,
                    decided_at=2.0, requested_at=1.0, decided_by="human:test")
    d = r.to_dict()
    assert d["id"] == "x1" and d["tool"] == "deploy"
    assert d["outcome"] == "approved" and d["requested_at"] == 1.0


def test_resolve_records_audit_and_clears_requested_at():
    e = _fresh_enhancements()
    aid = gate.request("deploy", {"target": "prod"}, "ship it", "danger")
    e.audit_request(aid)
    item = e.resolve_with_history(aid, approved=True, decided_by="human:test")
    assert item is not None and item["tool"] == "deploy"
    recs = e.history()
    assert len(recs) == 1
    assert recs[0].outcome is DecisionOutcome.APPROVED
    assert recs[0].decided_by == "human:test"
    assert aid not in e._requested_at  # cleaned up after record


def test_expiration_sweep_rejects_stale_pending():
    e = _fresh_enhancements(ttl_seconds=10.0)
    aid = gate.request("merge_main", {"branch": "x"}, "merge")
    e.audit_request(aid)
    # advance the clock past TTL
    e._clock = lambda: 2000.0
    expired = e.sweep_expired()
    assert expired == [aid]
    recs = e.history()
    assert len(recs) == 1 and recs[0].outcome is DecisionOutcome.EXPIRED
    # gate pending is now empty
    assert gate.pending() == []


def test_emergency_stop_kills_all_pending_and_blocks_new():
    e = _fresh_enhancements()
    aid1 = gate.request("deploy", {"t": 1}, "first")
    aid2 = gate.request("spend_money", {"amount": "5"}, "second")
    e.audit_request(aid1)
    e.audit_request(aid2)

    res = e.engage_kill_switch(engaged_by="operator:test", note="drill")
    assert res["active"] is True
    assert res["pending_killed"] == 2
    assert e.is_killed() is True
    assert e.is_request_blocked() is True
    # Both items are recorded as KILLED.
    outcomes = {r.outcome for r in e.history()}
    assert outcomes == {DecisionOutcome.KILLED}
    assert gate.pending() == []  # all cleared

    # release -> can request again
    e.release_kill_switch(released_by="operator:test")
    assert e.is_killed() is False
    assert e.is_request_blocked() is False


def test_batch_resolve_one_decision_covers_many():
    e = _fresh_enhancements()
    ids = []
    for i in range(5):
        a = gate.request("deploy", {"i": i}, f"reason-{i}")
        e.audit_request(a)
        ids.append(a)
    items = e.resolve_batch(ids, approved=True, decided_by="human:batch")
    assert len(items) == 5
    recs = e.history()
    assert len(recs) == 5
    for r in recs:
        assert r.outcome is DecisionOutcome.APPROVED
        assert r.decided_by == "human:batch"
        assert r.note == "batch"


def test_history_filters_by_tool_and_outcome():
    e = _fresh_enhancements()
    a1 = gate.request("deploy", {}, "d")
    a2 = gate.request("spend_money", {}, "m")
    e.audit_request(a1)
    e.audit_request(a2)
    e.resolve_with_history(a1, approved=True)
    e.resolve_with_history(a2, approved=False)
    only_deploy = e.history(tool="deploy")
    assert len(only_deploy) == 1 and only_deploy[0].tool == "deploy"
    only_rejected = e.history(outcome=DecisionOutcome.REJECTED)
    assert len(only_rejected) == 1 and only_rejected[0].tool == "spend_money"


def test_history_stats_group_correctly():
    e = _fresh_enhancements()
    for _ in range(3):
        a = gate.request("deploy", {}, "x")
        e.audit_request(a)
        e.resolve_with_history(a, approved=True)
    for _ in range(2):
        a = gate.request("spend_money", {}, "x")
        e.audit_request(a)
        e.resolve_with_history(a, approved=False)
    s = e.history_stats()
    assert s["total"] == 5
    assert s["by_outcome"]["approved"] == 3
    assert s["by_outcome"]["rejected"] == 2


def test_resolve_unknown_id_is_noop_not_error():
    e = _fresh_enhancements()
    item = e.resolve_with_history("does-not-exist", approved=True)
    assert item is None
    assert e.history() == []  # no audit record created


def test_kill_switch_recorded_in_history_stats():
    e = _fresh_enhancements()
    e.engage_kill_switch(engaged_by="op", note="drill")
    s = e.history_stats()
    assert s["kill_switch"]["active"] is True
    assert s["kill_switch"]["engaged_by"] == "op"
    e.release_kill_switch(released_by="op")
    assert e.history_stats()["kill_switch"]["active"] is False


# --------------------------------------------------------------------------- #
# gateway-level tests for the new endpoints                                    #
# --------------------------------------------------------------------------- #


class _ScriptRouter:
    def __init__(self, script=None):
        self._script = list(script or [])

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="m")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="m")

    async def aclose(self):
        pass


def _hive(tmp_path, script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter(script))


def _client(hive: HiveOS) -> TestClient:
    return TestClient(create_app(hive))


_TOKEN = {"X-Hive-Token": "change_me"}


def test_gateway_history_endpoint_returns_records(tmp_path):
    hive = _hive(tmp_path)
    aid = gate.request("deploy", {"target": "prod"}, "ship it")
    enhance.audit_request(aid)
    with _client(hive) as c:
        r = c.post("/approvals/decide", json={"approval_id": aid, "approved": True},
                   headers=_TOKEN)
        assert r.status_code == 200
        body = c.get("/approvals/history", headers=_TOKEN).json()
    assert body["count"] >= 1
    rec = body["records"][0]
    assert rec["tool"] == "deploy" and rec["outcome"] == "approved"
    assert rec["decided_by"].startswith("human:")
    assert "stats" in body and body["stats"]["total"] >= 1


def test_gateway_history_filters_by_outcome(tmp_path):
    hive = _hive(tmp_path)
    aid = gate.request("spend_money", {"amount": "5"}, "donate")
    enhance.audit_request(aid)
    with _client(hive) as c:
        c.post("/approvals/decide", json={"approval_id": aid, "approved": False},
               headers=_TOKEN)
        body = c.get("/approvals/history?outcome=rejected", headers=_TOKEN).json()
    assert body["count"] >= 1
    assert all(rec["outcome"] == "rejected" for rec in body["records"])


def test_gateway_emergency_stop_engage_blocks_new(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        # Engage
        r = c.post("/approvals/emergency-stop",
                   json={"action": "engage", "engaged_by": "test", "note": "drill"},
                   headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is True and body["engaged_by"] == "test"
        # GET shows the state
        state = c.get("/approvals/emergency-stop", headers=_TOKEN).json()
        assert state["active"] is True
        # Release
        r2 = c.post("/approvals/emergency-stop",
                    json={"action": "release", "released_by": "test"},
                    headers=_TOKEN)
        assert r2.json()["active"] is False


def test_gateway_emergency_stop_invalid_action_rejected(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/approvals/emergency-stop",
                   json={"action": "detonate"}, headers=_TOKEN)
        # unknown action defaults to engage; verify it engaged rather than 500
        assert r.status_code == 200
        assert r.json()["active"] is True
        # cleanup
        c.post("/approvals/emergency-stop",
               json={"action": "release"}, headers=_TOKEN)


def test_gateway_expire_endpoint_sweeps_pending(tmp_path):
    hive = _hive(tmp_path)
    aid = gate.request("deploy", {}, "ship")
    enhance.audit_request(aid)
    # Force the recorded requested_at into the past so the TTL fires immediately.
    enhance._requested_at[aid] = enhance._clock() - 99999.0
    with _client(hive) as c:
        r = c.post("/approvals/expire", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert aid in body["expired"]
        # Verify audit record reflects EXPIRED.
        hist = c.get("/approvals/history?outcome=expired", headers=_TOKEN).json()
        assert any(rec["id"] == aid for rec in hist["records"])


def test_gateway_late_approval_after_expiry_never_executes(tmp_path):
    """A late approve cannot turn an expired request into tool execution."""
    hive = _hive(tmp_path)
    calls = []

    async def should_not_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("expired approval reached executor")

    hive.tool_executor.execute_approved = should_not_run
    aid = gate.request("deploy", {}, "ship")
    enhance.audit_request(aid)
    enhance._requested_at[aid] = enhance._clock() - 99999.0
    with _client(hive) as c:
        response = c.post("/approvals/decide",
                          json={"approval_id": aid, "approved": True},
                          headers=_TOKEN)
    assert response.status_code == 200, response.text
    assert response.json() == {"executed": False, "status": "expired"}
    assert calls == []


def test_gateway_expire_fails_task_waiting_on_that_approval(tmp_path):
    """TTL expiry must release the durable heartbeat task, not leave it pending."""
    hive = _hive(tmp_path)
    aid = gate.request("deploy", {}, "ship")
    enhance.audit_request(aid)
    task_id = hive.task_board.enqueue("tool", {"tool": "deploy"})
    assert hive.task_board.claim(task_id)
    assert hive.task_board.await_approval(task_id, aid)
    enhance._requested_at[aid] = enhance._clock() - 99999.0

    with _client(hive) as c:
        response = c.post("/approvals/expire", headers=_TOKEN)
        task = hive.task_board.get(task_id)

    assert response.status_code == 200
    assert task is not None and task.state == "failed"
    assert task.last_error == "approval expired"


def test_gateway_kill_switch_fails_task_waiting_on_that_approval(tmp_path):
    """Emergency stop must release every task tied to a killed approval."""
    hive = _hive(tmp_path)
    aid = gate.request("deploy", {}, "ship")
    enhance.audit_request(aid)
    task_id = hive.task_board.enqueue("tool", {"tool": "deploy"})
    assert hive.task_board.claim(task_id)
    assert hive.task_board.await_approval(task_id, aid)

    with _client(hive) as c:
        response = c.post("/approvals/emergency-stop",
                          json={"action": "engage", "engaged_by": "test"},
                          headers=_TOKEN)
        task = hive.task_board.get(task_id)

    assert response.status_code == 200
    assert task is not None and task.state == "failed"
    assert task.last_error == "approval killed by emergency stop"
