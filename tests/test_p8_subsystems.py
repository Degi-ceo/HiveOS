"""P8 — budgeter gate, observability (telemetry/audit), self_mod dry-run, heartbeat tick."""
from __future__ import annotations

import asyncio

from hive.core.budgeter import Budgeter
from hive.core.events import EventBus, EventType
from hive.core.self_mod import SelfModifier
from hive.observability.audit import AuditLog
from hive.observability.telemetry import Telemetry


# --- budgeter ------------------------------------------------------------------

def test_budgeter_gate_blocks_at_cap():
    b = Budgeter(daily_cap=2)
    assert b.gate() == (True, "")
    b.record_call(); b.record_call()
    ok, why = b.gate()
    assert ok is False and "daily cap" in why
    assert b.snapshot()["calls_today"] == 2


def test_budgeter_rolls_over_day():
    t = [1000.0]
    b = Budgeter(daily_cap=1, clock=lambda: t[0])
    b.record_call()
    assert b.gate()[0] is False
    t[0] += 86_400 * 2          # next day
    assert b.gate()[0] is True


def test_budgeter_is_near_cap():
    b = Budgeter(daily_cap=10)
    assert b.is_near_cap() is False
    for _ in range(9):
        b.record_call()
    assert b.is_near_cap() is True  # 9/10 >= 0.9
    assert b.is_near_cap(threshold=1.0) is False  # 9/10 < 1.0
    b.record_call()
    assert b.is_near_cap(threshold=1.0) is True  # 10/10 == 1.0


# --- telemetry (EventBus subscriber) -------------------------------------------

def test_telemetry_counts_from_bus():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "output_tokens": 12})
    bus.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "output_tokens": 8})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "shell"})
    snap = tel.snapshot()
    assert snap["inference_calls"] == 2 and snap["output_tokens"] == 20
    assert snap["tool_calls"] == 1 and snap["by_model"]["MiniMax-M3"] == 2


# --- audit log (SQLite sink) ---------------------------------------------------

def test_audit_log_records_and_reads(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite")
    a.record({"tool": "shell", "status": "ok", "args": {"cmd": "echo hi"}})
    a.record({"tool": "deploy", "status": "pending_approval", "args": {}})
    recent = a.recent()
    assert {r["tool"] for r in recent} == {"shell", "deploy"}


def test_audit_log_prune_respects_max_rows(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite", max_rows=3)
    for i in range(6):
        a.record({"tool": f"tool{i}", "status": "ok", "args": {}})
    # After 6 records with max_rows=3 each record triggers a prune;
    # at most 3 rows should remain.
    recent = a.recent(limit=100)
    assert len(recent) <= 3


def test_audit_log_explicit_prune(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite", max_rows=100)
    for i in range(10):
        a.record({"tool": f"t{i}", "status": "ok", "args": {}})
    deleted = a.prune(max_rows=5)
    assert deleted == 5
    assert len(a.recent(limit=100)) == 5


# --- self_mod (dry-run + protected refusal) ------------------------------------

def _fake_runner(script):
    async def run(cmd, cwd=None):
        for key, rc, out in script:
            if key in cmd:
                return rc, out
        return 0, ""
    return run


def test_self_mod_dry_run_passes_without_push():
    runner = _fake_runner([("rev-parse", 0, "abc123\n"), ("worktree add", 0, ""),
                           ("pytest", 0, "1 passed"), ("worktree remove", 0, "")])
    pushed = []

    async def apply_fn(wt):
        return ["src/hive/llm/router.py"]            # non-protected change

    sm = SelfModifier(run=runner, test_cmd="python -m pytest -q")
    res = asyncio.run(sm.propose("t", "d", apply_fn, dry_run=True))
    assert res["ok"] is True and res["stage"] == "dry_run"
    assert res["last_good"] == "abc123"


def test_self_mod_refuses_protected_paths():
    runner = _fake_runner([("rev-parse", 0, "abc\n"), ("worktree add", 0, ""),
                           ("worktree remove", 0, "")])

    async def apply_fn(wt):
        return ["config/SOUL.md"]                    # PROTECTED

    sm = SelfModifier(run=runner)
    res = asyncio.run(sm.propose("t", "d", apply_fn, dry_run=True))
    assert res["ok"] is False and res["stage"] == "protected"


def test_self_mod_test_failure_stays_on_last_good():
    runner = _fake_runner([("rev-parse", 0, "good\n"), ("worktree add", 0, ""),
                           ("pytest", 1, "FAILED test"), ("worktree remove", 0, "")])

    async def apply_fn(wt):
        return ["src/hive/x.py"]

    sm = SelfModifier(run=runner, test_cmd="python -m pytest -q")
    res = asyncio.run(sm.propose("t", "d", apply_fn))
    assert res["ok"] is False and res["stage"] == "test" and res["last_good"] == "good"


# --- heartbeat tick ------------------------------------------------------------

def test_heartbeat_tick_plans_dispatches_consolidates(tmp_path):
    import json
    from hive.core.config import HiveConfig
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS
    from hive.autonomy.heartbeat import Heartbeat

    plan = [{"task": "check disk", "tool": "shell", "args": {"cmd": "echo ok"}, "reason": "r"}]

    class _Router:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            return CompletionResult(text="```json\n" + json.dumps(plan) + "\n```", model="m")
        async def aclose(self):
            pass

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_Router())
    hb = Heartbeat(hive, goals=["stay healthy"])
    summary = asyncio.run(hb.tick())
    assert summary["planned"] == 1 and summary["dispatched"] == 1
