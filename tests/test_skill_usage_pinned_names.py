"""SPRINT_6 P-I T2.3: pinned_names() on SkillUsageStore.

Unit-level test for the new method added to memory/skill_usage.py. Does not
touch the FastAPI gateway — covers the store contract directly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from hive.memory.skill_usage import SkillUsageStore


def _store(tmp_path: Path) -> SkillUsageStore:
    db = tmp_path / "skill_usage.db"
    return SkillUsageStore(db)


def test_pinned_names_empty_when_nothing_pinned(tmp_path):
    s = _store(tmp_path)
    s.register("alpha")
    s.register("beta")
    assert s.pinned_names() == []


def test_pinned_names_returns_only_pinned_sorted(tmp_path):
    s = _store(tmp_path)
    s.register("alpha", pinned=False)
    s.register("beta", pinned=True)
    s.register("gamma", pinned=True)
    s.register("delta", pinned=True)
    assert s.pinned_names() == ["beta", "delta", "gamma"]


def test_pinned_names_reflects_unpin(tmp_path):
    s = _store(tmp_path)
    s.register("zeta", pinned=True)
    s.register("eta", pinned=True)
    assert sorted(s.pinned_names()) == ["eta", "zeta"]
    s.unpin("zeta")
    assert s.pinned_names() == ["eta"]


def test_pinned_names_ignores_state(tmp_path):
    """Pinned is orthogonal to lifecycle state — even archived-pinned skills stay listed."""
    s = _store(tmp_path)
    s.register("theta", pinned=True)
    s.set_state("theta", "archived")
    assert s.pinned_names() == ["theta"]