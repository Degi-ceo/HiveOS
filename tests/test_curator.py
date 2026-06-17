"""M2 #si-2 — skill usage store + Curator lifecycle (offline, deterministic clock)."""
from __future__ import annotations

import pytest

from hive.memory.skill_usage import (
    STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, SkillUsageStore,
)
from hive.memory.curator import Curator, CuratorConfig

_DAY = 86_400.0


def _store(now):
    return SkillUsageStore(":memory:", clock=lambda: now[0])


# --- skill usage store ---------------------------------------------------------

def test_register_idempotent_and_get():
    now = [1000.0]
    s = _store(now)
    s.register("skill_a")
    s.register("skill_a")  # second call must not reset
    s.record_use("skill_a")
    s.register("skill_a")  # still idempotent after use
    u = s.get("skill_a")
    assert u is not None and u.use_count == 1 and u.state == STATE_ACTIVE


def test_record_use_autoregisters_and_bumps():
    now = [1000.0]
    s = _store(now)
    s.record_use("new_skill")
    s.record_use("new_skill")
    u = s.get("new_skill")
    assert u.use_count == 2 and u.last_used_ts == 1000.0


def test_record_use_reactivates_stale():
    now = [1000.0]
    s = _store(now)
    s.register("x")
    s.set_state("x", STATE_STALE)
    s.record_use("x")
    assert s.get("x").state == STATE_ACTIVE


def test_record_use_does_not_unarchive():
    now = [1000.0]
    s = _store(now)
    s.register("x")
    s.set_state("x", STATE_ARCHIVED, archived_ts=1000.0)
    s.record_use("x")  # usage alone must not resurrect an archived skill
    assert s.get("x").state == STATE_ARCHIVED


# --- curator transitions -------------------------------------------------------

def _curated(now, **cfg):
    store = _store(now)
    cur = Curator(store, config=CuratorConfig(**cfg), clock=lambda: now[0])
    return store, cur


def test_recent_skill_stays_active():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("fresh")
    now[0] = 10 * _DAY
    cur.run()
    assert store.get("fresh").state == STATE_ACTIVE


def test_idle_skill_goes_stale_then_archived():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("old")
    now[0] = 40 * _DAY            # idle 40d > stale(30)
    cur.run()
    assert store.get("old").state == STATE_STALE
    now[0] = 100 * _DAY           # idle 100d > archive(90)
    cur.run()
    assert store.get("old").state == STATE_ARCHIVED


def test_pinned_skill_is_exempt():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("keep")
    store.set_pinned("keep", True)
    now[0] = 200 * _DAY
    cur.run()
    assert store.get("keep").state == STATE_ACTIVE


def test_non_agent_created_is_skipped():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("bundled", agent_created=False)
    now[0] = 200 * _DAY
    cur.run()
    assert store.get("bundled").state == STATE_ACTIVE


def test_never_deletes_archived_row_persists():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("gone")
    now[0] = 365 * _DAY
    cur.run()
    u = store.get("gone")
    assert u is not None and u.state == STATE_ARCHIVED   # archived, NOT deleted
    assert u.archived_ts == 365 * _DAY


def test_restore_brings_back_archived():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("comeback")
    now[0] = 365 * _DAY
    cur.run()
    assert store.get("comeback").state == STATE_ARCHIVED
    assert cur.restore("comeback") is True
    assert store.get("comeback").state == STATE_ACTIVE


def test_restore_unknown_returns_false():
    now = [0.0]
    _, cur = _curated(now)
    assert cur.restore("nope") is False


def test_run_report_shape():
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("a")
    store.register("b")
    now[0] = 40 * _DAY
    report = cur.run()
    assert report["skills"] == 2
    assert len(report["transitions"]) == 2
    assert all(t["to_state"] == STATE_STALE for t in report["transitions"])


# --- pre-run backup ------------------------------------------------------------

def test_skill_usage_by_state(tmp_path):
    now = [0.0]
    s = _store(now)
    s.register("a")
    s.register("b")
    s.set_state("b", STATE_STALE)
    active = s.by_state(STATE_ACTIVE)
    stale = s.by_state(STATE_STALE)
    assert len(active) == 1 and active[0].name == "a"
    assert len(stale) == 1 and stale[0].name == "b"


def test_skill_usage_top_used():
    now = [0.0]
    s = _store(now)
    for skill in ["x", "y", "z"]:
        s.register(skill)
    s.record_use("z"); s.record_use("z"); s.record_use("z")
    s.record_use("x")
    top = s.top_used(limit=2)
    assert top[0].name == "z" and top[0].use_count == 3
    assert len(top) == 2


def test_skill_usage_stats():
    now = [0.0]
    s = _store(now)
    s.register("a")
    s.register("b")
    s.set_state("b", STATE_STALE)
    stats = s.stats()
    assert stats["total"] == 2
    assert stats["by_state"][STATE_ACTIVE] == 1
    assert stats["by_state"][STATE_STALE] == 1


def test_skill_usage_names(tmp_path):
    now = [0.0]
    s = _store(now)
    s.register("alpha")
    s.register("beta")
    s.set_state("beta", STATE_STALE)
    all_names = s.names()
    assert sorted(all_names) == ["alpha", "beta"]
    active_names = s.names(state=STATE_ACTIVE)
    assert active_names == ["alpha"]
    stale_names = s.names(state=STATE_STALE)
    assert stale_names == ["beta"]
    assert s.names(state="archived") == []


def test_skill_usage_delete(tmp_path):
    now = [0.0]
    s = _store(now)
    s.register("to_remove")
    s.register("keep")
    assert s.delete("to_remove") is True
    assert s.get("to_remove") is None
    assert s.delete("to_remove") is False   # already gone
    assert s.get("keep") is not None


def test_backup_written_before_transition(tmp_path):
    import json
    now = [0.0]
    store = _store(now)
    store.register("a")
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  backup_dir=tmp_path / "backups", clock=lambda: now[0])
    now[0] = 40 * _DAY
    report = cur.run()
    assert report["backup"] is not None
    backups = list((tmp_path / "backups").glob("skills-*.json"))
    assert len(backups) == 1
    data = json.loads(backups[0].read_text())
    # backup captures pre-transition state (still active at snapshot time)
    assert data[0]["name"] == "a" and data[0]["state"] == STATE_ACTIVE
