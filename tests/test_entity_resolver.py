"""SPRINT_7 Batch D — memory entity resolution (resolver + consolidation wiring).

Covers five normalisation tests, five resolution tests, five merge tests,
and five integration tests that exercise the MemoryKeeper's resolver-aware
consolidate() and the runtime/config wiring.

Mnemosyne itself may not be available in this test env; tests that touch the
live provider are gated behind ``pytest.mark.skipif`` so the suite stays green
when the extra is absent.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from hive.core.config import HiveConfig, set_config
from hive.memory.entity_resolver import (
    EntityResolver,
    ResolvedEntity,
)
from hive.memory.keeper import MemoryKeeper
from hive.memory.local import LocalMemoryProvider
from hive.memory.vault import ObsidianVault


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _provider(tmp_path, with_vault=False) -> LocalMemoryProvider:
    vault = ObsidianVault(tmp_path / "vault") if with_vault else None
    return LocalMemoryProvider(tmp_path / "mem.sqlite", vault=vault)


# ===========================================================================
# 5 normalisation tests
# ===========================================================================


def test_normalize_pr_95_variants():
    """All four surface forms collapse to one canonical key."""
    r = EntityResolver()
    keys = {r.canonical_key(s) for s in ["PR #95", "pr_95", "PR-95", "PR95"]}
    assert keys == {"pr95"}


def test_normalize_case_insensitive():
    """Case differences must not produce different canonical keys."""
    r = EntityResolver()
    assert r.canonical_key("PR-95") == r.canonical_key("pr-95")
    assert r.canonical_key("PR_95") == r.canonical_key("pR_95")


def test_normalize_strips_punctuation():
    """All non-word characters are stripped (digits and letters kept)."""
    r = EntityResolver()
    assert r.canonical_key("P.R.#.95") == "pr95"
    assert r.canonical_key("!!PR?95!!") == "pr95"
    assert r.canonical_key("pr---___95") == "pr95"


def test_normalize_collapses_whitespace():
    """Whitespace runs collapse to nothing in the canonical form."""
    r = EntityResolver()
    assert r.canonical_key("PR 95") == "pr95"
    assert r.canonical_key("  P   R   9  5  ") == "pr95"
    assert r.canonical_key("PR\t\n95") == "pr95"


def test_normalize_unicode_safe():
    """NFKD-normalisation strips diacritics; unicode inputs are safe."""
    r = EntityResolver()
    # žižek with carons -> zizek (NFKD + combining-mark strip)
    assert r.canonical_key("Žižek") == "zizek"
    # accented latin name keeps its word chars under NFKD
    assert r.canonical_key("Kamilski") == "kamilski"
    # Polish "Łukasz": Ł stays Ł (separate unicode letter, kept by \w).
    assert r.canonical_key("Łukasz") == "łukasz"
    # cyrillic letters pass through (they're word chars after NFKD lowercase).
    assert r.canonical_key("Привет") == "привет"


# ===========================================================================
# 5 resolution tests
# ===========================================================================


def test_resolve_returns_canonical_and_aliases():
    """A fresh surface returns canonical_key + aliases with itself recorded."""
    r = EntityResolver()
    resolved = r.resolve("PR-95")
    assert resolved.canonical_key == "pr95"
    assert "PR-95" in resolved.aliases
    assert resolved.confidence == 1.0
    assert resolved.is_alias_match is False


def test_resolve_accumulates_aliases_across_calls():
    """resolve() must return every observed surface form for a canonical key,
    not just the current call's surface (regression: the deduped/observed
    list was computed and discarded, returning only [surface])."""
    r = EntityResolver()
    r.resolve("PR #95")
    resolved = r.resolve("pr_95")
    assert resolved.canonical_key == "pr95"
    assert set(resolved.aliases) == {"PR #95", "pr_95"}


def test_resolve_with_alias_map_override():
    """An alias map overrides normalisation: 'kamil' -> 'operator'."""
    r = EntityResolver(alias_map={"kamil": "operator"})
    resolved = r.resolve("Kamil")
    assert resolved.canonical_key == "operator"
    # alias-map hit records the override, not the observed surface
    assert resolved.confidence == 0.9
    assert resolved.is_alias_match is True


def test_resolve_confidence_1_for_normalized():
    """A pure-normalisation match always returns confidence 1.0."""
    r = EntityResolver()
    assert r.resolve("PR-95").confidence == 1.0
    assert r.resolve("pr95").confidence == 1.0
    assert r.resolve("  PR  95  ").confidence == 1.0


def test_resolve_confidence_lower_for_alias_match():
    """Alias-match confidence is strictly less than normalisation confidence."""
    # Use an alias-map source that doesn't normalise to anything else, so the
    # alias branch is taken regardless of how the surface form is presented.
    r = EntityResolver(alias_map={"hiveos": "hiveos100"})
    normed = r.resolve("hiveos_v1").confidence          # fresh normalisation
    aliased = r.resolve("hiveos").confidence             # alias-map hit
    assert aliased < normed
    assert aliased == pytest.approx(0.9)
    assert normed == pytest.approx(1.0)


def test_resolve_empty_string():
    """Empty / pure-punctuation surfaces do not raise; they return empty key."""
    r = EntityResolver()
    for empty in ("", " ", "!!!", "---"):
        res = r.resolve(empty)
        assert res.canonical_key == ""
        assert res.confidence == 1.0
        assert res.aliases == [] or res.aliases == [empty]


# ===========================================================================
# 5 merge tests
# ===========================================================================


def test_merge_groups_by_canonical_key():
    """4 surface forms of the same entity must collapse to 1 group."""
    r = EntityResolver()
    facts = [
        {"id": "m1", "subject": "PR #95", "data": {"status": "merged"}},
        {"id": "m2", "subject": "pr_95", "data": {"ci": "green"}},
        {"id": "m3", "subject": "PR-95", "data": {"comments": 7}},
        {"id": "m4", "subject": "PR95", "data": {"author": "kamil"}},
    ]
    merged = r.merge(facts)
    assert merged["group_count"] == 1
    assert merged["fact_count"] == 4
    group = merged["groups"][0]
    assert group["canonical_key"] == "pr95"
    assert group["count"] == 4


def test_merge_preserves_aliases_list():
    """Every distinct surface form is recorded in the group's aliases list."""
    r = EntityResolver()
    facts = [
        {"id": "1", "subject": "PR-95", "data": {}},
        {"id": "2", "subject": "pr_95", "data": {}},
        {"id": "3", "subject": "PR95", "data": {}},
    ]
    merged = r.merge(facts)
    aliases = merged["groups"][0]["aliases"]
    assert "PR-95" in aliases
    assert "pr_95" in aliases
    assert "PR95" in aliases
    # de-duped (insertion order)
    assert len(aliases) == 3


def test_merge_keeps_fact_ids():
    """All fact ids are accumulated under the group, no duplicates."""
    r = EntityResolver()
    facts = [
        {"id": 101, "subject": "PR-95", "data": {}},
        {"id": 202, "subject": "pr_95", "data": {}},
        {"id": 303, "subject": "PR95", "data": {}},
    ]
    merged = r.merge(facts)
    assert merged["groups"][0]["fact_ids"] == [101, 202, 303]


def test_merge_merges_data_dicts():
    """Data dicts are deep-merged (per-key union, lists concatenated by value)."""
    r = EntityResolver()
    facts = [
        {"id": "1", "subject": "PR-95", "data": {"meta": {"status": "merged"}, "tags": ["a"]}},
        {"id": "2", "subject": "pr_95", "data": {"meta": {"ci": "green"}, "tags": ["b"]}},
    ]
    merged = r.merge(facts)
    data = merged["groups"][0]["data"]
    assert data["meta"] == {"status": "merged", "ci": "green"}
    # list union (no duplicate "a", "b" appear once)
    assert sorted(data["tags"]) == ["a", "b"]


def test_merge_empty_input():
    """Empty input returns the zero-result envelope, not an error."""
    r = EntityResolver()
    out_empty = r.merge([])
    out_none = r.merge(None)
    assert out_empty == {"groups": [], "group_count": 0, "fact_count": 0}
    assert out_none == {"groups": [], "group_count": 0, "fact_count": 0}


# ===========================================================================
# 5 integration tests
# ===========================================================================


def test_mnemosyne_consolidate_uses_resolver(tmp_path):
    """MemoryKeeper.consolidate() with the default flag collapses aliases."""
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("hi", "hi back", session_id="s1")

    captured: list[str] = []

    async def fake_summarize(messages, system):
        return "```json\n" + json.dumps([
            {"kind": "fact", "topic": "PR #95", "content": "shipped", "source": "s1"},
            {"kind": "fact", "topic": "pr_95", "content": "duplicate", "source": "s1"},
            {"kind": "fact", "topic": "PR-95", "content": "third dup", "source": "s1"},
        ]) + "\n```"

    # Wrap learn so we can record the topics actually persisted.
    original_learn = p.learn

    def _track_learn(kind, topic, content, source=""):
        captured.append(topic)
        original_learn(kind, topic, content, source)

    p.learn = _track_learn
    keeper = MemoryKeeper(fake_summarize, p)

    new = asyncio.run(keeper.consolidate("s1"))
    # Only 1 fact persisted (canonical "pr95"), even though 3 aliases existed.
    assert new == 1
    # The persisted topic is the canonical key, not any single surface form.
    assert any("pr95" in t for t in captured)


def test_mnemosyne_consolidate_disabled_falls_back(tmp_path):
    """use_entity_resolution=False preserves the pre-Batch-D behaviour.

    Verified by checking that the per-item surface form is passed to learn()
    verbatim (no canonicalisation), as evidenced by the captured topic list.
    """
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("hi", "hi back", session_id="s1")

    captured: list[str] = []
    original_learn = p.learn

    def _track_learn(kind, topic, content, source=""):
        captured.append(topic)
        original_learn(kind, topic, content, source)

    p.learn = _track_learn

    async def fake_summarize(messages, system):
        # Distinct, unrelated topics — none of them should collapse together
        # regardless of resolution setting.
        return "```json\n" + json.dumps([
            {"kind": "fact", "topic": "unique-alpha-7Q3", "content": "shipped", "source": "s1"},
            {"kind": "fact", "topic": "unique-beta-2K9", "content": "duplicate", "source": "s1"},
        ]) + "\n```"

    keeper = MemoryKeeper(fake_summarize, p)
    new = asyncio.run(keeper.consolidate("s1", use_entity_resolution=False))
    # Without resolution, each surface is treated as a distinct fact.
    assert new == 2
    # No canonicalisation happened — every recorded topic is a raw surface form.
    assert captured == ["unique-alpha-7Q3", "unique-beta-2K9"]


def test_heartbeat_consolidation_path(tmp_path):
    """the heartbeat's path -> HiveOS.consolidate() defaults to entity resolution."""
    # Stand up a minimal HiveOS-equivalent flow: just verify the default flag.
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("hi", "hi back", session_id="s1")

    async def fake_summarize(messages, system):
        return "```json\n" + json.dumps([
            {"kind": "fact", "topic": "PR #95", "content": "x", "source": "s1"},
            {"kind": "fact", "topic": "PR_95", "content": "x", "source": "s1"},
        ]) + "\n```"

    keeper = MemoryKeeper(fake_summarize, p)
    # Simulate the heartbeat tick: call consolidate() with no override.
    new = asyncio.run(keeper.consolidate("s1"))
    # Heartbeat path defaults to entity resolution -> 1 item persisted.
    assert new == 1


def test_alias_map_loaded_from_env(tmp_path, monkeypatch):
    """config.entity_resolution_alias_map is parsed from HIVE_ENTITY_RESOLUTION_ALIAS_MAP."""
    monkeypatch.setenv("HIVE_ENTITY_RESOLUTION_ALIAS_MAP",
                       '{"Kamil": "operator", "hiveos_v1": "hiveos100"}')
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.entity_resolution_enabled is True
    assert "Kamil" in cfg.entity_resolution_alias_map
    # parsed via _load_entity_alias_map
    alias_data = json.loads(cfg.entity_resolution_alias_map)
    assert alias_data["Kamil"] == "operator"


def test_config_disabled_flag(monkeypatch):
    """entity_resolution_enabled=False disables the feature globally."""
    monkeypatch.setenv("HIVE_ENTITY_RESOLUTION_ENABLED", "false")
    cfg = HiveConfig.from_env(load_dotenv=False)
    assert cfg.entity_resolution_enabled is False
    # Re-enable for downstream tests so we don't leak state.
    monkeypatch.delenv("HIVE_ENTITY_RESOLUTION_ENABLED", raising=False)


# ===========================================================================
# Bonus: a few thin sanity tests for the public surface
# ===========================================================================


def test_resolved_entity_dataclass_is_frozen():
    """ResolvedEntity is frozen — callers should not mutate it."""
    ent = ResolvedEntity(canonical_key="pr95", aliases=["PR-95"])
    with pytest.raises(Exception):
        ent.canonical_key = "different"  # type: ignore[misc]


def test_keeper_accepts_explicit_resolver():
    """MemoryKeeper lets callers inject a pre-built resolver (advanced wiring)."""
    custom = EntityResolver(alias_map={"kamil": "operator"})
    keeper = MemoryKeeper(summarize=lambda *a: "", provider=None, resolver=custom)
    assert keeper.resolver is custom
    assert keeper.resolver.canonical_key("Kamil") == "operator"
