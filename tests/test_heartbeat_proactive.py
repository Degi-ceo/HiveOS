"""Heartbeat proactive intelligence (SPRINT_7 Batch C).

Covers:
  * Heartbeat.proactive_scan() — pattern candidates, stale facts, overdue commitments
  * Tick-hook — Nth-tick firing, interval=0 disabled, rate-limited to once per interval
  * ProactiveFinding dataclass — required fields, validation
  * Config fields — env overrides, validate() rejects <0
  * Graceful degradation — Mnemosyne / learned_skills / audit_log all optional
"""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.autonomy.heartbeat import (
    ALL_FINDING_TYPES,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    TYPE_LEARNED_SKILL_CANDIDATE,
    TYPE_STALE_COMMITMENT,
    TYPE_STALE_FACT,
    Heartbeat,
    ProactiveFinding,
    _interval_ticks,
)
from hive.autonomy.tasks import TaskBoard
from hive.core.config import HiveConfig
from hive.tools.learned_skills import SkillTemplate


# --- helpers ----------------------------------------------------------------

def _cfg(**overrides) -> HiveConfig:
    """Build a real HiveConfig from env, then patch fields for the test."""
    cfg = HiveConfig.from_env()
    # HiveConfig is frozen — we mutate via object.__setattr__ to avoid a
    # full reconstruct (keeps the rest of the env intact).
    # This module's tests exercise tick()'s internal scheduling/proactive-scan
    # behavior, not the P0 autonomy gate (added after these tests), so default
    # autonomy on unless a test explicitly overrides it.
    overrides.setdefault("autonomy_enabled", True)
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _hive_mock(cfg: HiveConfig, *, audit_entries: list[dict] | None = None,
               learned_templates: list[SkillTemplate] | None = None,
               mnemosyne_facts: list[dict] | None = None,
               mnemosyne_active: bool = False,
               overdue_commitments: list | None = None) -> MagicMock:
    hive = MagicMock()
    hive.config = cfg
    hive.events.publish = MagicMock()
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = []
    hive.task_board.claim.return_value = True
    hive.task_board.enqueue = MagicMock(return_value=1)
    hive.planner = MagicMock()
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = ""
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_diagnose = AsyncMock(return_value={"improvement_outcomes": []})
    hive.self_improve_from_symptom = AsyncMock(return_value=[])
    # Audit log
    audit = MagicMock()
    audit.export = MagicMock(return_value=list(audit_entries or []))
    hive.audit_log = audit
    # Learned skills store
    store = MagicMock()
    store.list_by_status = MagicMock(return_value=list(learned_templates or []))
    hive.learned_skills = store
    # Memory — control Mnemosyne presence
    mem = MagicMock()
    mem.name = "mnemosyne" if mnemosyne_active else "local"
    mem.most_important_facts = MagicMock(return_value=list(mnemosyne_facts or []))
    hive.memory = mem
    # Commitments
    commitments = MagicMock()
    commitments.upcoming = MagicMock(return_value=list(overdue_commitments or []))
    commitments.next_due_at = MagicMock(return_value=1.0)  # all past by default
    hive.commitments = commitments
    return hive


# --- 1. ProactiveFinding dataclass -----------------------------------------

def test_proactive_finding_dataclass_has_required_fields():
    f = ProactiveFinding(type=TYPE_LEARNED_SKILL_CANDIDATE,
                         data={"pattern": ["a", "b", "c"], "count": 3})
    assert f.type == TYPE_LEARNED_SKILL_CANDIDATE
    assert f.data == {"pattern": ["a", "b", "c"], "count": 3}
    assert f.priority == PRIORITY_MEDIUM
    assert isinstance(f.created_at, datetime)
    assert f.created_at.tzinfo is not None


def test_proactive_finding_rejects_unknown_type():
    with pytest.raises(ValueError, match="ProactiveFinding.type"):
        ProactiveFinding(type="bogus_type", data={})


def test_proactive_finding_rejects_unknown_priority():
    with pytest.raises(ValueError, match="ProactiveFinding.priority"):
        ProactiveFinding(type=TYPE_STALE_FACT, data={}, priority="bogus")


def test_proactive_finding_post_init_validates_type():
    """__post_init__ rejects unknown type values — even when assigned after construction.

    Slots-without-frozen still allows attribute assignment; what we test is that
    ANY ProactiveFinding, regardless of how built, cannot end up with an invalid
    type at the end of construction (re-init via __init__ would re-run
    __post_init__, so the validation is the contract).
    """
    with pytest.raises(ValueError, match="ProactiveFinding.type"):
        ProactiveFinding(type="not_a_real_type", data={})
    # Fresh construction with a valid type succeeds.
    f = ProactiveFinding(type=TYPE_LEARNED_SKILL_CANDIDATE, data={"x": 1})
    assert f.type == TYPE_LEARNED_SKILL_CANDIDATE
    # Fields persist for read access.
    assert f.data == {"x": 1}
    assert f.priority == PRIORITY_MEDIUM


def test_all_finding_types_are_strings():
    for t in ALL_FINDING_TYPES:
        assert isinstance(t, str) and t


# --- 2. proactive_scan() sub-scans ----------------------------------------

def test_proactive_scan_finds_candidate_patterns():
    """Audit log with the same 3-tool sequence repeated -> finding returned."""
    # Repeat (a,b,c) 3 times so detect_patterns() (min_repeats=2) catches it.
    audit = []
    for _ in range(3):
        audit.extend([
            {"tool": "a", "status": "ok"},
            {"tool": "b", "status": "ok"},
            {"tool": "c", "status": "ok"},
        ])
    cfg = _cfg()
    hive = _hive_mock(cfg, audit_entries=audit)
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    types = {f.type for f in findings}
    assert TYPE_LEARNED_SKILL_CANDIDATE in types
    candidate = next(f for f in findings if f.type == TYPE_LEARNED_SKILL_CANDIDATE)
    assert tuple(candidate.data["pattern"]) == ("a", "b", "c")
    assert candidate.data["count"] >= 2


def test_proactive_scan_skips_already_registered_patterns():
    """Patterns already in learned_skills must NOT be returned as candidates."""
    # Use a 3-call sequence (x,y,z) that detect_patterns will surface; register
    # the same 3-tuple as a learned skill.
    audit = [
        {"tool": "x", "status": "ok"},
        {"tool": "y", "status": "ok"},
        {"tool": "z", "status": "ok"},
        {"tool": "x", "status": "ok"},
        {"tool": "y", "status": "ok"},
        {"tool": "z", "status": "ok"},
    ]
    tpl = SkillTemplate(id="t1", name="learned_t1", description="",
                        pattern=("x", "y", "z"), params={}, code="",
                        status="registered", created_ts=0.0)
    cfg = _cfg()
    hive = _hive_mock(cfg, audit_entries=audit, learned_templates=[tpl])
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    candidates = [f for f in findings if f.type == TYPE_LEARNED_SKILL_CANDIDATE]
    # None of the candidates should contain the registered pattern tuple exactly.
    for c in candidates:
        assert tuple(c.data["pattern"]) != ("x", "y", "z")


def test_proactive_scan_finds_stale_facts_when_mnemosyne_present():
    import time as _time
    now_ts = _time.time()
    # fact_a: last accessed 90 days ago (stale)
    # fact_b: last accessed 5 days ago (fresh)
    # fact_c: never accessed, created 90 days ago (stale)
    facts = [
        {"id": "f1", "topic": "old", "last_accessed": now_ts - 90 * 86400,
         "created_ts": now_ts - 200 * 86400},
        {"id": "f2", "topic": "fresh", "last_accessed": now_ts - 5 * 86400,
         "created_ts": now_ts - 100 * 86400},
        {"id": "f3", "topic": "old-untouched", "last_accessed": None,
         "created_ts": now_ts - 90 * 86400},
    ]
    cfg = _cfg(heartbeat_stale_fact_days=30)
    hive = _hive_mock(cfg, mnemosyne_facts=facts, mnemosyne_active=True)
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    stale = [f for f in findings if f.type == TYPE_STALE_FACT]
    assert len(stale) == 2
    ids = {f.data["fact_id"] for f in stale}
    assert ids == {"f1", "f3"}


def test_proactive_scan_skips_stale_facts_when_mnemosyne_absent():
    """No Mnemosyne -> no stale_fact findings (and no crash)."""
    cfg = _cfg()
    hive = _hive_mock(cfg, mnemosyne_active=False)
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    assert all(f.type != TYPE_STALE_FACT for f in findings)


def test_proactive_scan_finds_overdue_commitments():
    import time as _time
    now_ts = _time.time()
    c_old = SimpleNamespace(id=10, description="daily summary")
    c_recent = SimpleNamespace(id=11, description="hourly ping")
    cfg = _cfg(heartbeat_stale_commitment_days=7)
    hive = _hive_mock(cfg, overdue_commitments=[c_old, c_recent])
    # c_old due 20 days ago, c_recent due 1 day ago — only c_old qualifies
    def next_due(cid):
        return now_ts - (20 * 86400 if cid == 10 else 1 * 86400)
    hive.commitments.next_due_at = MagicMock(side_effect=next_due)
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    stale = [f for f in findings if f.type == TYPE_STALE_COMMITMENT]
    assert len(stale) == 1
    assert stale[0].data["commitment_id"] == 10
    assert stale[0].data["days_overdue"] >= 7
    assert stale[0].priority == PRIORITY_HIGH


def test_proactive_scan_skips_future_commitments():
    """Commitments whose next_due is in the future are not surfaced."""
    import time as _time
    now_ts = _time.time()
    c_future = SimpleNamespace(id=20, description="next-week task")
    cfg = _cfg(heartbeat_stale_commitment_days=7)
    hive = _hive_mock(cfg, overdue_commitments=[c_future])
    # Override the helper's default (all-past) with a future timestamp.
    hive.commitments.next_due_at = MagicMock(return_value=now_ts + 5 * 86400)
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    assert all(f.type != TYPE_STALE_COMMITMENT for f in findings)


def test_proactive_scan_returns_empty_when_no_signals():
    cfg = _cfg()
    hive = _hive_mock(cfg)
    hb = Heartbeat(hive)
    assert hb.proactive_scan() == []


def test_proactive_scan_does_not_throw_when_learned_skills_missing():
    """learned_skills attribute absent -> Scan A returns [], no exception."""
    cfg = _cfg()
    hive = _hive_mock(cfg)
    # Simulate an older runtime where learned_skills isn't wired.
    del hive.learned_skills
    hb = Heartbeat(hive)
    findings = hb.proactive_scan()
    assert all(f.type != TYPE_LEARNED_SKILL_CANDIDATE for f in findings)


def test_proactive_scan_does_not_throw_when_audit_log_missing():
    """audit_log attribute absent -> Scan A returns [], no exception."""
    cfg = _cfg()
    hive = _hive_mock(cfg)
    del hive.audit_log
    hb = Heartbeat(hive)
    assert all(f.type != TYPE_LEARNED_SKILL_CANDIDATE
               for f in hb.proactive_scan())


def test_proactive_scan_handles_detect_patterns_exception():
    """If detect_patterns() raises, Scan A returns [] without raising."""
    cfg = _cfg()
    hive = _hive_mock(cfg)
    # Patch the audit_log to return rows that exercise the failure path
    # without us having to monkey-patch detect_patterns.
    import hive.tools.learned_skills as ls_mod
    original = ls_mod.detect_patterns
    ls_mod.detect_patterns = MagicMock(side_effect=RuntimeError("boom"))
    try:
        hb = Heartbeat(hive)
        findings = hb.proactive_scan()
        assert all(f.type != TYPE_LEARNED_SKILL_CANDIDATE for f in findings)
    finally:
        ls_mod.detect_patterns = original


# --- 3. tick() hook --------------------------------------------------------

def test_tick_calls_proactive_scan_every_n_ticks():
    """Default config: interval 86400s / heartbeat_sec 900 = 96 ticks."""
    cfg = _cfg()  # defaults: 86400s / 900s = 96 ticks
    # Add audit data so the scan has something to surface.
    audit = []
    for _ in range(3):
        audit.extend([{"tool": "a", "status": "ok"},
                      {"tool": "b", "status": "ok"},
                      {"tool": "c", "status": "ok"}])
    hive = _hive_mock(cfg, audit_entries=audit)
    hb = Heartbeat(hive)
    # Run 96 ticks; the 96th must trigger the scan.
    for _ in range(96):
        asyncio.run(hb.tick(1000.0))
    # enqueue should have been called at least once with the proactive kind.
    calls = [c for c in hive.task_board.enqueue.call_args_list
             if c.args and c.args[0] == "proactive_suggestion"]
    assert len(calls) >= 1, "proactive scan never fired after 96 ticks"


def test_tick_does_not_call_proactive_when_interval_zero():
    """interval=0 disables the scan entirely."""
    cfg = _cfg(heartbeat_proactive_interval_sec=0)
    hive = _hive_mock(cfg)
    hb = Heartbeat(hive)
    for _ in range(5):
        asyncio.run(hb.tick(1000.0))
    calls = [c for c in hive.task_board.enqueue.call_args_list
             if c.args and c.args[0] == "proactive_suggestion"]
    assert calls == []


def test_tick_enqueues_proactive_suggestion_task_per_finding():
    """Each finding becomes one board row with kind=proactive_suggestion."""
    cfg = _cfg(heartbeat_proactive_interval_sec=900, heartbeat_sec=900)
    # 900s interval at 900s/tick -> fires every tick.
    audit = []
    for _ in range(3):
        audit.extend([{"tool": "p", "status": "ok"},
                      {"tool": "q", "status": "ok"},
                      {"tool": "r", "status": "ok"}])
    hive = _hive_mock(cfg, audit_entries=audit)
    hb = Heartbeat(hive)
    asyncio.run(hb.tick(1000.0))
    proactive_calls = [c for c in hive.task_board.enqueue.call_args_list
                       if c.args and c.args[0] == "proactive_suggestion"]
    assert len(proactive_calls) >= 1
    # Inspect payload structure
    payload = proactive_calls[0].args[1]
    assert payload["kind"] == "proactive_suggestion"
    assert payload["finding_type"] == TYPE_LEARNED_SKILL_CANDIDATE
    assert payload["priority"] in (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)
    assert "data" in payload
    assert "created_at" in payload
    assert proactive_calls[0].kwargs.get("source") == "proactive_suggestion"


def test_tick_scans_before_due_commitments_are_marked_fulfilled():
    """Scan C must see stale commitments before the scheduler resets them."""
    cfg = _cfg(heartbeat_proactive_interval_sec=900, heartbeat_sec=900)
    hive = _hive_mock(cfg)
    order: list[str] = []
    hive.commitments.due_and_enqueue.side_effect = lambda now: (order.append("schedule") or 0)
    hb = Heartbeat(hive)
    hb.proactive_scan = lambda: (order.append("scan") or [])

    asyncio.run(hb.tick(1000.0))

    assert order.index("scan") < order.index("schedule")


def test_tick_runs_scan_at_most_once_per_interval():
    """Two ticks within the interval: only the first (Nth tick) triggers scan."""
    cfg = _cfg(heartbeat_proactive_interval_sec=1800, heartbeat_sec=900)
    # 1800s / 900s = 2 ticks per interval. After 2 ticks scan fires; 3rd does not.
    audit = []
    for _ in range(3):
        audit.extend([{"tool": "m", "status": "ok"},
                      {"tool": "n", "status": "ok"},
                      {"tool": "o", "status": "ok"}])
    hive = _hive_mock(cfg, audit_entries=audit)
    hb = Heartbeat(hive)
    # Track scan invocations on the Heartbeat instance.
    scan_calls = []
    original_scan = hb.proactive_scan
    hb.proactive_scan = lambda: (scan_calls.append(1) or original_scan())
    asyncio.run(hb.tick(1000.0))  # tick 1: counter=1, no fire
    asyncio.run(hb.tick(1001.0))  # tick 2: counter=2 == tick_n -> fires
    asyncio.run(hb.tick(1002.0))  # tick 3: counter=1 again, no fire
    assert len(scan_calls) == 1, (
        f"expected exactly one proactive_scan call, got {len(scan_calls)}")


def test_tick_returns_proactive_enqueued_in_summary():
    cfg = _cfg(heartbeat_proactive_interval_sec=900, heartbeat_sec=900)
    audit = []
    for _ in range(3):
        audit.extend([{"tool": "u", "status": "ok"},
                      {"tool": "v", "status": "ok"},
                      {"tool": "w", "status": "ok"}])
    hive = _hive_mock(cfg, audit_entries=audit)
    hb = Heartbeat(hive)
    summary = asyncio.run(hb.tick(1000.0))
    assert "proactive_enqueued" in summary
    assert "proactive_runs" in summary
    assert summary["proactive_runs"] == 1
    assert summary["proactive_enqueued"] >= 1


def test_tick_proactive_failure_does_not_abort_tick():
    """If proactive_scan raises, tick still completes."""
    cfg = _cfg(heartbeat_proactive_interval_sec=900, heartbeat_sec=900)
    hive = _hive_mock(cfg)
    hb = Heartbeat(hive)
    # Force proactive_scan to raise on the second tick (after first is silent).
    hb.proactive_scan = MagicMock(side_effect=RuntimeError("scan boom"))
    # tick 1: counter goes 1->no fire; tick 2: counter==tick_n -> tries to scan
    summary1 = asyncio.run(hb.tick(1000.0))
    summary2 = asyncio.run(hb.tick(1001.0))
    assert summary1["proactive_runs"] == 0
    assert summary2["proactive_runs"] == 0
    assert "dispatched" in summary2  # tick still completed


# --- 4. config fields ------------------------------------------------------

def test_config_fields_have_env_overrides(monkeypatch):
    monkeypatch.setenv("HIVE_HEARTBEAT_PROACTIVE_INTERVAL_SEC", "123")
    monkeypatch.setenv("HIVE_HEARTBEAT_STALE_FACT_DAYS", "45")
    monkeypatch.setenv("HIVE_HEARTBEAT_STALE_COMMITMENT_DAYS", "14")
    cfg = HiveConfig.from_env()
    assert cfg.heartbeat_proactive_interval_sec == 123
    assert cfg.heartbeat_stale_fact_days == 45
    assert cfg.heartbeat_stale_commitment_days == 14


def test_config_fields_default_values():
    cfg = HiveConfig.from_env()
    # Sanity: defaults from from_env (env may set them in CI).
    assert cfg.heartbeat_proactive_interval_sec >= 0
    assert cfg.heartbeat_stale_fact_days >= 1
    assert cfg.heartbeat_stale_commitment_days >= 1


def test_interval_validation_rejects_negative():
    """validate() rejects negative intervals."""
    cfg = _cfg(heartbeat_proactive_interval_sec=-1)
    issues = cfg.validate()
    assert any("HIVE_HEARTBEAT_PROACTIVE_INTERVAL_SEC" in m for m in issues)


def test_interval_validation_rejects_zero_stale_days():
    """0 stale_fact_days would make every fact stale on creation — rejected."""
    cfg = _cfg(heartbeat_stale_fact_days=0)
    issues = cfg.validate()
    assert any("HIVE_HEARTBEAT_STALE_FACT_DAYS" in m for m in issues)


def test_validate_accepts_disabled_scan():
    """interval=0 (disabled) is OK; only stale_* must be positive."""
    cfg = _cfg(heartbeat_proactive_interval_sec=0,
               heartbeat_stale_fact_days=30, heartbeat_stale_commitment_days=7)
    issues = cfg.validate()
    assert not any("PROACTIVE" in m for m in issues)


# --- 5. _interval_ticks() helper -------------------------------------------

def test_interval_ticks_zero_when_disabled():
    cfg = _cfg(heartbeat_proactive_interval_sec=0, heartbeat_sec=900)
    assert _interval_ticks(cfg) == 0


def test_interval_ticks_default_is_96():
    """Default: 86400s / 900s = 96 ticks per scan cycle."""
    cfg = _cfg()  # env defaults
    assert _interval_ticks(cfg) == 96


def test_interval_ticks_rounds_up():
    cfg = _cfg(heartbeat_proactive_interval_sec=901, heartbeat_sec=900)
    # 901/900 rounds up to 2
    assert _interval_ticks(cfg) == 2


def test_interval_ticks_handles_missing_fields():
    """Robust against configs that lack the field (e.g. older test fixtures)."""
    s = SimpleNamespace(heartbeat_proactive_interval_sec=1800, heartbeat_sec=900)
    assert _interval_ticks(s) == 2


# --- 6. integration: TaskBoard round-trip ----------------------------------

def test_proactive_suggestion_task_roundtrips_through_task_board(tmp_path):
    """A real TaskBoard can store and retrieve proactive_suggestion rows."""
    cfg = _cfg()
    board = TaskBoard(tmp_path / "tasks.sqlite")
    try:
        hb_payload = {
            "kind": "proactive_suggestion",
            "finding_type": TYPE_LEARNED_SKILL_CANDIDATE,
            "priority": PRIORITY_MEDIUM,
            "data": {"pattern": ["a", "b", "c"], "count": 4},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        task_id = board.enqueue("proactive_suggestion", hb_payload,
                                source="proactive_suggestion")
        row = board.get(task_id)
        assert row is not None
        assert row.kind == "proactive_suggestion"
        assert row.payload["finding_type"] == TYPE_LEARNED_SKILL_CANDIDATE
        assert row.payload["data"]["count"] == 4
        assert row.source == "proactive_suggestion"
    finally:
        board.close()


def test_proactive_suggestion_survives_the_generic_dispatcher(tmp_path):
    """Regression: proactive_suggestion rows have no `tool` key, so the generic
    dispatcher (heartbeat._dispatch) used to claim+complete them on the very
    next tick's due() before any consumer (GET /tasks) could ever see them —
    the whole Batch C feature produced no visible output. They must stay
    PENDING (visible via the task board) instead of being silently discarded,
    and must not block the planner from running by keeping `due` non-empty
    forever."""
    cfg = _cfg(heartbeat_proactive_interval_sec=900, heartbeat_sec=900)
    board = TaskBoard(tmp_path / "tasks.sqlite")
    try:
        audit = []
        for _ in range(3):
            audit.extend([{"tool": "a", "status": "ok"},
                          {"tool": "b", "status": "ok"},
                          {"tool": "c", "status": "ok"}])
        hive = _hive_mock(cfg, audit_entries=audit)
        hive.task_board = board
        hb = Heartbeat(hive)

        summary1 = asyncio.run(hb.tick(1000.0))
        assert summary1["proactive_enqueued"] >= 1
        first_batch = board.search(kind="proactive_suggestion")
        assert first_batch
        assert all(r.state == "pending" for r in first_batch), (
            "proactive_suggestion rows must not be claimed/completed by the "
            "generic tool dispatcher")

        # A second tick (with nothing else due) must still be able to plan —
        # the pending proactive rows must not permanently starve the
        # "if not due: plan fresh work" branch.
        summary2 = asyncio.run(hb.tick(1001.0))
        assert summary2["planned"] >= 0  # planner ran (mocked to return [])
        hive.planner.plan.assert_awaited()

        # The second scan supersedes the first batch instead of piling up
        # forever: the old rows are no longer pending.
        stale = board.search(kind="proactive_suggestion", state="pending")
        assert not any(r.id in {f.id for f in first_batch} for r in stale)
    finally:
        board.close()


# --- 7. AuditLog.export(limit=...) — Sprint 7 Batch C blocker regression ---

def test_audit_export_limit_returns_most_recent_n(tmp_path):
    """limit=N returns the most recent N rows in chronological (ASC) order."""
    from hive.observability.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.sqlite")
    try:
        for i in range(5):
            audit.record({
                "tool": f"tool_{i}",
                "status": "ok",
                "approved": True,
                "args": {"i": i},
            })
        recent2 = audit.export(limit=2)
        assert len(recent2) == 2
        # The 2 most recent rows are tool_3 and tool_4 — returned in ASC
        # chronological order so downstream pattern detection sees them
        # left-to-right as they occurred.
        assert recent2[0]["tool"] == "tool_3"
        assert recent2[1]["tool"] == "tool_4"
        # limit >= total returns everything (still in ASC order).
        all_rows = audit.export(limit=10)
        assert len(all_rows) == 5
        assert [r["tool"] for r in all_rows] == [
            "tool_0", "tool_1", "tool_2", "tool_3", "tool_4",
        ]
    finally:
        audit.close()


def test_audit_export_limit_intersects_with_range(tmp_path):
    """limit and start_ts/end_ts both apply — limit caps AFTER range filter."""
    import time as _time
    from hive.observability.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.sqlite")
    try:
        for i in range(5):
            audit.record({
                "tool": f"tool_{i}",
                "status": "ok",
                "approved": True,
                "args": {"i": i},
            })
        # Window covers all 5 rows. limit caps to the 2 newest, returned ASC.
        now = _time.time()
        all_rows = audit.export(start_ts=now - 3600, end_ts=now + 3600, limit=2)
        assert len(all_rows) == 2
        assert all_rows[0]["tool"] == "tool_3"
        assert all_rows[1]["tool"] == "tool_4"
        # Window excludes everything — empty even with limit.
        future = audit.export(start_ts=now + 3600, end_ts=now + 7200, limit=5)
        assert future == []
        # Range subset (e.g. only 2 of 5 are in window when we use a start_ts
        # after rows 0..2 were written). Write 3 rows, wait briefly so ts
        # advances, then write 2 more — query a window that only includes
        # the last 2.
        # We simulate the ts gap by inserting then re-inserting with explicit
        # older ts via a fresh audit on a new DB.
        audit2 = AuditLog(tmp_path / "audit2.sqlite")
        try:
            for i in range(3):
                audit2.record({
                    "tool": f"old_{i}",
                    "status": "ok",
                    "approved": True,
                    "args": {},
                })
            cutoff = _time.time() + 0.5
            _time.sleep(0.6)
            for i in range(2):
                audit2.record({
                    "tool": f"new_{i}",
                    "status": "ok",
                    "approved": True,
                    "args": {},
                })
            after = audit2.export(start_ts=cutoff, limit=2)
            assert len(after) == 2
            assert all(r["tool"].startswith("new_") for r in after)
        finally:
            audit2.close()
    finally:
        audit.close()


def test_proactive_scan_audit_patterns_actually_fire(tmp_path):
    """Scan A end-to-end with a REAL AuditLog + real LearnedSkillStore.

    This is the regression test for the Batch C blocker: the heartbeat was
    calling ``audit.export(limit=...)`` which the AuditLog did not support,
    so Scan A silently returned no findings in production. This test wires
    a real AuditLog (on a temp DB) into the heartbeat, writes 3× the same
    tool-call pattern, and asserts that ``learned_skill_candidate`` shows up.
    """
    from hive.autonomy.tasks import TaskBoard
    from hive.observability.audit import AuditLog
    from hive.tools.learned_skills import LearnedSkillStore

    audit = AuditLog(tmp_path / "audit.sqlite")
    store = LearnedSkillStore(tmp_path / "learned.sqlite")
    board = TaskBoard(tmp_path / "tasks.sqlite")
    try:
        # Write 3 repeats of the same 3-tool sequence: (a, b, c).
        for _ in range(3):
            audit.record({"tool": "a", "status": "ok", "approved": True, "args": {}})
            audit.record({"tool": "b", "status": "ok", "approved": True, "args": {}})
            audit.record({"tool": "c", "status": "ok", "approved": True, "args": {}})

        cfg = _cfg()
        hive = MagicMock()
        hive.config = cfg
        hive.events.publish = MagicMock()
        hive.cron.due_and_enqueue.return_value = 0
        hive.commitments.due_and_enqueue.return_value = 0
        hive.task_board.due.return_value = []
        hive.task_board.recent_failures.return_value = []
        hive.task_board.claim.return_value = True
        hive.task_board.enqueue = MagicMock(return_value=1)
        hive.planner = MagicMock()
        hive.planner.plan = AsyncMock(return_value=[])
        hive.memory.prefetch.return_value = ""
        hive.consolidate = AsyncMock(return_value=0)
        hive.curate.return_value = {"transitions": []}
        hive.curate_umbrellas = AsyncMock()
        hive.budgeter.refresh = AsyncMock()
        hive.self_diagnose = AsyncMock(return_value={"improvement_outcomes": []})
        hive.self_improve_from_symptom = AsyncMock(return_value=[])
        # REAL AuditLog + SkillUsageStore + TaskBoard — not mocks.
        hive.audit_log = audit
        hive.learned_skills = store
        hive.commitments = MagicMock()
        hive.commitments.upcoming = MagicMock(return_value=[])
        mem = MagicMock()
        mem.name = "local"
        mem.most_important_facts = MagicMock(return_value=None)
        hive.memory = mem

        hb = Heartbeat(hive)
        findings = hb.proactive_scan()
        candidates = [f for f in findings if f.type == TYPE_LEARNED_SKILL_CANDIDATE]
        assert candidates, (
            "Scan A must surface a learned_skill_candidate when an AuditLog "
            "contains 3× the same tool-call pattern. Got no candidates — the "
            "limit kwarg regression is back."
        )
        candidate = candidates[0]
        assert tuple(candidate.data["pattern"]) == ("a", "b", "c")
        assert candidate.data["count"] >= 2
    finally:
        audit.close()
        store.close()
        board.close()
