"""SPRINT_6 P-I T5.1: /health/summary.channels per-channel status."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("HIVE_SECRET", "change_me")
os.environ.setdefault("HIVE_HOME", tempfile.mkdtemp(prefix="hive-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from hive.gateway.app import create_app  # noqa: E402
from hive.runtime import HiveOS  # noqa: E402
from hive.core.config import HiveConfig  # noqa: E402
from tests.test_gateway import _ScriptRouter  # noqa: E402

_TOKEN = {"X-Hive-Token": "change_me"}


def _hive(tmp_path: Path) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter([]))


def test_health_summary_includes_channels(tmp_path):
    hive = _hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.get("/health/summary", headers=_TOKEN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "channels" in body
    assert set(body["channels"].keys()) >= {"telegram", "slack", "discord", "email"}
    # Each channel reports a bool (active / not active).
    for name, val in body["channels"].items():
        assert isinstance(val, bool), f"{name}: expected bool, got {type(val)}"
