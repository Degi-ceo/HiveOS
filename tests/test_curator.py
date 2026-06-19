"""M2 #si-2 — skill usage store + Curator lifecycle (offline, deterministic clock)."""
from __future__ import annotations

import asyncio
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


def test_skill_usage_recently_used(tmp_path):
    now = [100.0]
    store = _store(now)
    store.register("a")
    store.register("b")
    store.register("c")
    now[0] = 200.0
    store.record_use("b")
    now[0] = 300.0
    store.record_use("c")
    now[0] = 150.0
    store.record_use("a")
    recent = store.recently_used(limit=10)
    # c used at 300, a at 150, b at 200 — wait, newest first
    names = [s.name for s in recent]
    assert names[0] == "c"  # most recently used


def test_skill_usage_recently_used_excludes_unused():
    store = SkillUsageStore(":memory:")
    store.register("used")
    store.register("never_used")
    store.record_use("used")
    recent = store.recently_used()
    assert len(recent) == 1
    assert recent[0].name == "used"


def test_skill_usage_recently_used_empty():
    store = SkillUsageStore(":memory:")
    assert store.recently_used() == []


def test_skill_usage_pin_and_unpin():
    store = SkillUsageStore(":memory:")
    store.register("pinnable")
    assert store.pin("pinnable") is True
    assert store.get("pinnable").pinned is True
    assert store.unpin("pinnable") is True
    assert store.get("pinnable").pinned is False


def test_skill_usage_pin_unknown_returns_false():
    store = SkillUsageStore(":memory:")
    assert store.pin("nonexistent") is False
    assert store.unpin("nonexistent") is False


def test_skill_usage_unused_skills_empty():
    store = SkillUsageStore(":memory:")
    assert store.unused_skills() == []


def test_skill_usage_unused_skills_returns_never_used():
    store = SkillUsageStore(":memory:")
    store.register("used_skill")
    store.record_use("used_skill")
    store.register("unused_skill")
    unused = store.unused_skills()
    assert len(unused) == 1
    assert unused[0].name == "unused_skill"


def test_skill_usage_unused_skills_excludes_archived():
    store = SkillUsageStore(":memory:")
    store.register("a")
    store.set_state("a", "archived")
    # Archived skills with use_count=0 should NOT appear in unused_skills
    assert store.unused_skills() == []


def test_skill_usage_archived_count_zero_initially():
    store = SkillUsageStore(":memory:")
    assert store.archived_count() == 0


def test_skill_usage_archived_count_increments():
    store = SkillUsageStore(":memory:")
    store.register("a")
    store.register("b")
    store.register("c")
    store.set_state("a", "archived")
    store.set_state("b", "archived")
    assert store.archived_count() == 2


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


# --- consolidate_umbrellas + _parse_umbrellas (G-11) ---------------------------

def test_parse_umbrellas_valid():
    from hive.memory.curator import _parse_umbrellas
    result = _parse_umbrellas('[{"name": "search", "covers": ["a", "b"]}]')
    assert len(result) == 1
    assert result[0]["name"] == "search"
    assert result[0]["covers"] == ["a", "b"]


def test_parse_umbrellas_fenced():
    from hive.memory.curator import _parse_umbrellas
    raw = '```json\n[{"name": "ops", "covers": ["x", "y"]}]\n```'
    result = _parse_umbrellas(raw)
    assert len(result) == 1 and result[0]["name"] == "ops"


def test_parse_umbrellas_invalid():
    from hive.memory.curator import _parse_umbrellas
    assert _parse_umbrellas("not json at all") == []
    assert _parse_umbrellas('{"object": "not-a-list"}') == []
    assert _parse_umbrellas("") == []


def test_consolidate_umbrellas_no_summarizer():
    now = [1000.0]
    store = _store(now)
    cur = Curator(store)  # no summarize injected
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("skipped") is True


def test_consolidate_umbrellas_below_threshold():
    now = [1000.0]
    store = _store(now)
    for name in ("a", "b", "c"):
        store.register(name, agent_created=True)

    async def _summarize(messages, system):
        return "[]"

    cur = Curator(store, summarize=_summarize)
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("skipped") is True


def test_consolidate_umbrellas_creates_umbrella():
    now = [1000.0]
    store = _store(now)
    for name in ("search-web", "fetch-url", "crawl-site", "scrape-html", "get-page"):
        store.register(name, agent_created=True)

    async def _summarize(messages, system):
        return '[{"name": "web-access", "covers": ["search-web", "fetch-url", "crawl-site"]}]'

    cur = Curator(store, summarize=_summarize, clock=lambda: now[0])
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("created") == 1
    assert result.get("archived") == 3
    umbrella = store.get("web-access")
    assert umbrella is not None and umbrella.pinned is True
    # source skills archived
    assert store.get("search-web").state == STATE_ARCHIVED
    assert store.get("fetch-url").state == STATE_ARCHIVED
    assert store.get("crawl-site").state == STATE_ARCHIVED


def test_consolidate_umbrellas_fail_open():
    now = [1000.0]
    store = _store(now)
    for name in ("a", "b", "c", "d", "e"):
        store.register(name, agent_created=True)

    async def _bad_summarize(messages, system):
        raise RuntimeError("LLM unavailable")

    cur = Curator(store, summarize=_bad_summarize)
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("skipped") is True
    assert "reason" in result


# --- new edge-case tests -------------------------------------------------------

def test_curator_run_archives_old_stale_skills():
    """A skill already in STATE_STALE that exceeds archive_after_days is archived."""
    now = [0.0]
    store = _store(now)
    # Register and advance to stale threshold so first run makes it stale
    store.register("old_skill")
    now[0] = 40 * _DAY   # > stale_after_days=30
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  clock=lambda: now[0])
    cur.run()
    assert store.get("old_skill").state == STATE_STALE

    # Advance past archive threshold — next run must archive it
    now[0] = 100 * _DAY  # > archive_after_days=90
    cur.run()
    assert store.get("old_skill").state == STATE_ARCHIVED


def test_curator_run_returns_transitions_list():
    """run() always returns a dict with a 'transitions' key that is a list."""
    now = [0.0]
    store = _store(now)
    store.register("skill_x")
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  clock=lambda: now[0])
    # No time advance — no transitions expected
    report = cur.run()
    assert "transitions" in report
    assert isinstance(report["transitions"], list)

    # After advancing past stale threshold some transitions appear
    now[0] = 40 * _DAY
    report = cur.run()
    assert isinstance(report["transitions"], list)
    assert len(report["transitions"]) >= 1


def test_curator_backup_creates_file(tmp_path):
    """When backup_dir is set, run() writes a backup file and returns its path."""
    now = [0.0]
    store = _store(now)
    store.register("backed_up_skill")
    backup_dir = tmp_path / "skill_backups"
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  backup_dir=backup_dir, clock=lambda: now[0])
    report = cur.run()
    assert report["backup"] is not None, "backup path should be set when backup_dir is provided"
    backup_files = list(backup_dir.glob("skills-*.json"))
    assert len(backup_files) == 1, f"Expected exactly one backup file, got: {backup_files}"


# --- Additional curator tests ---------------------------------------------------

def test_curator_stale_after_overrideable():
    """CuratorConfig.stale_after_days can be customized."""
    config = CuratorConfig(stale_after_days=7, archive_after_days=14)
    assert config.stale_after_days == 7
    assert config.archive_after_days == 14


def test_skill_usage_all_returns_list():
    """SkillUsageStore.all() returns a list (possibly empty)."""
    now = [0.0]
    store = _store(now)
    result = store.all()
    assert isinstance(result, list)


def test_skill_usage_register_creates_active_entry():
    """Registering a new skill creates it in ACTIVE state."""
    from hive.memory.skill_usage import STATE_ACTIVE
    now = [0.0]
    store = _store(now)
    store.register("test_skill")
    entry = store.get("test_skill")
    assert entry is not None
    assert entry.state == STATE_ACTIVE


def test_curator_run_report_has_transitions_key():
    """The run() report always has a 'transitions' key."""
    now = [0.0]
    store = _store(now)
    cur = Curator(store, clock=lambda: now[0])
    report = cur.run()
    assert "transitions" in report


def test_skill_usage_top_used_empty():
    """top_used() with no records returns empty list."""
    now = [0.0]
    store = _store(now)
    result = store.top_used(limit=5)
    assert isinstance(result, list) and len(result) == 0


def test_skill_usage_names_returns_registered():
    """names() returns name of registered skill."""
    now = [0.0]
    store = _store(now)
    store.register("alpha_skill")
    assert "alpha_skill" in store.names()


def test_skill_usage_stats_returns_dict():
    """stats() returns a dict with count information."""
    now = [0.0]
    store = _store(now)
    result = store.stats()
    assert isinstance(result, dict)


def test_skill_usage_recently_used_respects_limit():
    """recently_used(limit=2) returns at most 2 entries."""
    now = [0.0]
    store = _store(now)
    for name in ["a", "b", "c", "d"]:
        store.register(name)
        store.record_use(name)
    result = store.recently_used(limit=2)
    assert len(result) <= 2


# --- New tests: additional behavioral coverage --------------------------------

def test_curator_run_skills_count_matches_registered():
    """run() report 'skills' key equals the number of registered skills."""
    now = [0.0]
    store = _store(now)
    for name in ("x1", "x2", "x3"):
        store.register(name)
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  clock=lambda: now[0])
    report = cur.run()
    assert report["skills"] == 3


def test_skill_usage_set_state_to_active():
    """set_state() can explicitly set a stale skill back to active."""
    now = [0.0]
    store = _store(now)
    store.register("toggler")
    store.set_state("toggler", STATE_STALE)
    assert store.get("toggler").state == STATE_STALE
    store.set_state("toggler", STATE_ACTIVE)
    assert store.get("toggler").state == STATE_ACTIVE


def test_curator_pinned_skill_not_in_transitions():
    """A pinned skill must never appear in the transitions list even after long idle."""
    now = [0.0]
    store = _store(now)
    store.register("pinned_skill")
    store.set_pinned("pinned_skill", True)
    cur = Curator(store, config=CuratorConfig(stale_after_days=1, archive_after_days=2),
                  clock=lambda: now[0])
    now[0] = 200 * _DAY
    report = cur.run()
    transitioned_names = {t["name"] for t in report["transitions"]}
    assert "pinned_skill" not in transitioned_names


def test_curator_non_agent_created_not_in_transitions():
    """A non-agent-created skill must never appear in the transitions list."""
    now = [0.0]
    store = _store(now)
    store.register("bundled_tool", agent_created=False)
    cur = Curator(store, config=CuratorConfig(stale_after_days=1, archive_after_days=2),
                  clock=lambda: now[0])
    now[0] = 200 * _DAY
    report = cur.run()
    transitioned_names = {t["name"] for t in report["transitions"]}
    assert "bundled_tool" not in transitioned_names


def test_skill_usage_all_count_after_register():
    """all() returns one entry per registered skill."""
    now = [0.0]
    store = _store(now)
    store.register("alpha")
    store.register("beta")
    store.register("gamma")
    entries = store.all()
    assert len(entries) == 3
    names = {e.name for e in entries}
    assert names == {"alpha", "beta", "gamma"}


def test_curator_restore_active_skill_returns_true():
    """restore() returns True even for a skill that is already active (not archived)."""
    now = [0.0]
    store = _store(now)
    store.register("always_active")
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  clock=lambda: now[0])
    # restore() works on any existing skill, including one that was never transitioned
    assert cur.restore("always_active") is True
    assert store.get("always_active").state == STATE_ACTIVE


# --- 6 new unique tests -------------------------------------------------------

def test_run_transition_dict_has_required_keys():
    """Each entry in run()['transitions'] has 'name', 'from_state', and 'to_state'."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("aging")
    now[0] = 40 * _DAY
    report = cur.run()
    assert len(report["transitions"]) == 1
    t = report["transitions"][0]
    assert set(t.keys()) >= {"name", "from_state", "to_state"}
    assert t["name"] == "aging"
    assert t["from_state"] == STATE_ACTIVE
    assert t["to_state"] == STATE_STALE


def test_run_multiple_skills_mixed_states():
    """run() processes each skill independently; three outcome states in a single run."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("old_stale")    # will go stale (registered at t=0, no use)
    store.register("old_archived") # will go archived (registered at t=0, no use)
    store.register("fresh")        # will stay active (use recorded at t=40d)

    # Pre-seed old_archived so idle time > archive threshold: set last_used way back.
    # We can force this by directly setting its state to stale first, then advancing
    # past the archive threshold while old_stale only crosses the stale threshold.
    # Simplest: use two separate runs. First run at 40d marks both stale.
    # Then record_use on old_stale to reset its clock. Second run at 100d archives
    # old_archived (stale since t=0, idle=100d > 90d) but old_stale was used at 40d
    # (idle at 100d is 60d — above stale again but not archive).
    now[0] = 40 * _DAY
    store.record_use("fresh")      # fresh used at 40d: idle=0 when run happens
    cur.run()                      # old_stale & old_archived both go stale; fresh stays

    assert store.get("old_stale").state == STATE_STALE
    assert store.get("old_archived").state == STATE_STALE
    assert store.get("fresh").state == STATE_ACTIVE

    store.record_use("old_stale")  # old_stale used at 40d; resets its clock
    now[0] = 100 * _DAY
    store.record_use("fresh")      # fresh used again at 100d
    report2 = cur.run()

    # old_archived: last used at t=0, idle=100d > archive(90d) → archived
    assert store.get("old_archived").state == STATE_ARCHIVED
    # old_stale: record_use at 40d reactivated it; at 100d idle=60d > stale(30d) → stale again
    assert store.get("old_stale").state == STATE_STALE
    assert store.get("fresh").state == STATE_ACTIVE
    # Two transitions: old_archived archived, old_stale goes stale from active
    names = {t["name"] for t in report2["transitions"]}
    assert "old_archived" in names
    assert "fresh" not in names


def test_consolidate_umbrellas_below_threshold_with_summarizer():
    """consolidate_umbrellas() skips and never calls summarizer when < min_narrow skills."""
    now = [1000.0]
    store = _store(now)
    for name in ("alpha", "beta", "gamma", "delta"):   # 4 < default min_narrow=5
        store.register(name, agent_created=True)

    called = []

    async def _summarize(messages, system):
        called.append(True)
        return '[{"name": "all", "covers": ["alpha", "beta"]}]'

    cur = Curator(store, summarize=_summarize, clock=lambda: now[0])
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("skipped") is True
    assert not called, "summarizer must not be invoked when below threshold"


def test_skill_store_active_stale_archived_transition_sequence():
    """SkillUsageStore state machine: active -> stale -> archived via explicit set_state."""
    store = SkillUsageStore(":memory:")
    store.register("journey")
    assert store.get("journey").state == STATE_ACTIVE

    store.set_state("journey", STATE_STALE)
    assert store.get("journey").state == STATE_STALE

    store.set_state("journey", STATE_ARCHIVED, archived_ts=9999.0)
    u = store.get("journey")
    assert u.state == STATE_ARCHIVED
    assert u.archived_ts == 9999.0


def test_store_all_reflects_state_after_curator_run():
    """store.all() after run() returns entries whose states match post-transition values."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("goes_stale")
    store.register("stays_active")
    # record a use on stays_active to reset its idle clock (use_count only; idle is time-based)
    # Actually idle is time-based from last_used_ts; record_use updates last_used_ts
    store.record_use("stays_active")
    # Advance: stays_active has last_used_ts at 0 but we record_use at now=0 too;
    # we need stays_active to have a recent use — advance time first then record_use
    now[0] = 35 * _DAY
    store.record_use("stays_active")   # resets last_used_ts to 35*_DAY
    now[0] = 40 * _DAY
    cur.run()
    entries = {e.name: e.state for e in store.all()}
    assert entries["goes_stale"] == STATE_STALE
    assert entries["stays_active"] == STATE_ACTIVE


def test_pin_via_store_pin_method_exempts_from_curator():
    """store.pin() exempts a skill from Curator transitions (equivalent to set_pinned)."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=1, archive_after_days=2)
    store.register("precious")
    store.pin("precious")
    now[0] = 500 * _DAY
    report = cur.run()
    assert store.get("precious").state == STATE_ACTIVE
    assert all(t["name"] != "precious" for t in report["transitions"])


# --- Wave 4G-B additional curator tests -----------------------------------------

def test_wave4g_consolidate_umbrellas_exact_min_narrow():
    """consolidate_umbrellas with exactly min_narrow skills calls the summarizer."""
    now = [1000.0]
    store = _store(now)
    # Register exactly min_narrow=5 skills — should pass the threshold check.
    for name in ("s1", "s2", "s3", "s4", "s5"):
        store.register(name, agent_created=True)

    called = []

    async def _summarize(messages, system):
        called.append(True)
        return "[]"  # no umbrellas produced — that's fine

    cur = Curator(store, summarize=_summarize, clock=lambda: now[0])
    result = asyncio.run(cur.consolidate_umbrellas(min_narrow=5))
    # With exactly min_narrow skills the summarizer must be called (not skipped).
    assert called, "summarizer must be called when skill count equals min_narrow"
    # No umbrellas were returned, so result should indicate that.
    assert result.get("skipped") is True
    assert "no umbrellas" in result.get("reason", "")


def test_wave4g_backup_dir_creates_file_on_run(tmp_path):
    """Curator with backup_dir writes a backup JSON file every time run() is called."""
    import json

    now = [0.0]
    store = _store(now)
    store.register("skill_one")
    store.register("skill_two")
    backup_dir = tmp_path / "wb_backups"
    cur = Curator(store,
                  config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  backup_dir=backup_dir,
                  clock=lambda: now[0])

    now[0] = 10 * _DAY
    report = cur.run()

    assert report["backup"] is not None
    backup_files = list(backup_dir.glob("skills-*.json"))
    assert len(backup_files) == 1
    data = json.loads(backup_files[0].read_text())
    assert isinstance(data, list)
    assert len(data) == 2
    names_in_backup = {entry["name"] for entry in data}
    assert names_in_backup == {"skill_one", "skill_two"}


def test_wave4g_backup_captures_pre_transition_state(tmp_path):
    """Backup must capture the state *before* transitions (not after)."""
    import json

    now = [0.0]
    store = _store(now)
    store.register("transitions_soon")
    backup_dir = tmp_path / "pre_trans_backups"
    cur = Curator(store,
                  config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  backup_dir=backup_dir,
                  clock=lambda: now[0])

    now[0] = 40 * _DAY   # exceeds stale threshold: skill will become STALE after run()
    cur.run()

    # Post-run: skill is now STALE
    assert store.get("transitions_soon").state == STATE_STALE

    # Backup was written before the transition; skill must have been ACTIVE then.
    backup_files = list(backup_dir.glob("skills-*.json"))
    data = json.loads(backup_files[0].read_text())
    entry = next(e for e in data if e["name"] == "transitions_soon")
    assert entry["state"] == STATE_ACTIVE


def test_wave4g_run_no_backup_dir_returns_none_backup():
    """Without backup_dir, run() returns backup=None in the report."""
    now = [0.0]
    store = _store(now)
    store.register("sk")
    cur = Curator(store, config=CuratorConfig(stale_after_days=30, archive_after_days=90),
                  clock=lambda: now[0])
    report = cur.run()
    assert report["backup"] is None


def test_wave4g_curator_active_to_stale_transition_key_values():
    """Transition dict from ACTIVE to STALE has correct from/to state values."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("will_stale")
    now[0] = 40 * _DAY
    report = cur.run()
    t = next(t for t in report["transitions"] if t["name"] == "will_stale")
    assert t["from_state"] == STATE_ACTIVE
    assert t["to_state"] == STATE_STALE


def test_wave4g_curator_archived_skill_skipped_on_subsequent_run():
    """Once a skill is ARCHIVED, subsequent curator runs leave it unchanged."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("ancient")
    now[0] = 100 * _DAY   # > archive threshold
    cur.run()
    assert store.get("ancient").state == STATE_ARCHIVED

    # Second run: archived skill must not appear in transitions.
    now[0] = 200 * _DAY
    report2 = cur.run()
    transitioned = {t["name"] for t in report2["transitions"]}
    assert "ancient" not in transitioned
    assert store.get("ancient").state == STATE_ARCHIVED


def test_wave4g_consolidate_umbrellas_archives_covered_skills():
    """Each skill listed in 'covers' is archived after a successful consolidation."""
    now = [1000.0]
    store = _store(now)
    for name in ("read-file", "write-file", "delete-file", "copy-file", "move-file"):
        store.register(name, agent_created=True)

    async def _summarize(messages, system):
        return '[{"name": "file-ops", "covers": ["read-file", "write-file", "delete-file"]}]'

    cur = Curator(store, summarize=_summarize, clock=lambda: now[0])
    result = asyncio.run(cur.consolidate_umbrellas())
    assert result.get("created") == 1
    assert result.get("archived") == 3
    assert store.get("read-file").state == STATE_ARCHIVED
    assert store.get("write-file").state == STATE_ARCHIVED
    assert store.get("delete-file").state == STATE_ARCHIVED
    # Skills not in covers remain active.
    assert store.get("copy-file").state == STATE_ACTIVE
    assert store.get("move-file").state == STATE_ACTIVE


def test_wave4g_restore_stale_skill_returns_active():
    """restore() transitions a STALE skill back to ACTIVE, not just ARCHIVED ones."""
    now = [0.0]
    store, cur = _curated(now, stale_after_days=30, archive_after_days=90)
    store.register("drifted")
    now[0] = 40 * _DAY
    cur.run()
    assert store.get("drifted").state == STATE_STALE

    assert cur.restore("drifted") is True
    assert store.get("drifted").state == STATE_ACTIVE
