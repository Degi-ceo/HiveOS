"""SPRINT_6 P-I T2.3: ?pinned=true filter on /skills.

Adds three tests:
- /skills?pinned=true returns {"pinned": [...names...]} including only pinned skills
- /skills (no query) still returns the stats dict (regression — total/by_state shape)
- POST /skills/{name}/state transitions a skill to archived and back
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Headless test env before any imports that read env vars.
os.environ.setdefault("HIVE_SECRET", "change_me")
os.environ.setdefault("HIVE_HOME", tempfile.mkdtemp(prefix="hive-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from hive.gateway.app import create_app  # noqa: E402
from hive.runtime import HiveOS  # noqa: E402
from hive.gateway.protocol import PROTOCOL_VERSION  # noqa: E402  (not used, keeps parity)
from tests.test_gateway import _ScriptRouter  # noqa: E402

_TOKEN = {"X-Hive-Token": "change_me"}


def _hive(tmp_path: Path) -> HiveOS:
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter([]))


def test_skills_pinned_query_returns_only_pinned(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("alpha", pinned=False)
    hive.skill_usage.register("beta", pinned=True)
    hive.skill_usage.register("gamma", pinned=True)
    with TestClient(create_app(hive)) as c:
        r = c.get("/skills?pinned=true", headers=_TOKEN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pinned" in body
    assert sorted(body["pinned"]) == ["beta", "gamma"]


def test_skills_no_query_returns_stats_unchanged(tmp_path):
    hive = _hive(tmp_path)
    before = hive.skill_usage.stats()["total"]
    hive.skill_usage.register("alpha")
    with TestClient(create_app(hive)) as c:
        r = c.get("/skills", headers=_TOKEN)
    assert r.status_code == 200, r.text
    body = r.json()
    # stats() shape: {"total": N, "by_state": {...}} — relative assertion so the
    # test is robust against accumulated rows from previous tests sharing the
    # tmp_path fixture.
    assert "total" in body and "by_state" in body
    assert body["total"] == before + 1
    assert body["by_state"].get("active", 0) >= 1


def test_skill_state_endpoint_archives_then_restores(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("delta")
    with TestClient(create_app(hive)) as c:
        # archive
        r = c.post("/skills/delta/state", json={"state": "archived"}, headers=_TOKEN)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "archived"
        # restore
        r = c.post("/skills/delta/state", json={"state": "active"}, headers=_TOKEN)
        assert r.status_code == 200
        assert r.json()["state"] == "active"
        assert r.json()["archived_ts"] is None


def test_skill_state_rejects_invalid_state(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("epsilon")
    with TestClient(create_app(hive)) as c:
        r = c.post("/skills/epsilon/state", json={"state": "bogus"}, headers=_TOKEN)
    assert r.status_code == 400


def test_skill_state_404_for_unknown_skill(tmp_path):
    hive = _hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.post("/skills/missing/state", json={"state": "archived"}, headers=_TOKEN)
    assert r.status_code == 404