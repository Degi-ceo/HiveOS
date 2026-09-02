from __future__ import annotations

import dataclasses
import sqlite3

from hive.core.config import HiveConfig
from hive.core.pilot_doctor import inspect


def _configured(tmp_path) -> HiveConfig:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return dataclasses.replace(
        cfg,
        telegram_token="test-token",
        telegram_webhook_secret="test-secret",
        telegram_allowed_user_ids=frozenset({"7"}),
    )


def test_pilot_doctor_is_read_only_and_reports_ready_aggregate_state(tmp_path):
    cfg = _configured(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    sqlite3.connect(cfg.state_db).close()
    before = cfg.state_db.read_bytes()

    report = inspect(cfg)

    assert report == {
        "status": "ready", "state_integrity_ok": True,
        "telegram_ingress_configured": True, "autonomy_disabled": True,
        "reviews": {"memory": 0, "telegram": 0, "total": 0},
    }
    assert cfg.state_db.read_bytes() == before


def test_pilot_doctor_requires_owner_review_without_exposing_records(tmp_path):
    cfg = _configured(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(cfg.state_db)
    try:
        connection.executescript("""
        CREATE TABLE memory_projection_outbox(state TEXT NOT NULL);
        CREATE TABLE telegram_updates(state TEXT NOT NULL);
        INSERT INTO memory_projection_outbox VALUES('requires_review');
        INSERT INTO telegram_updates VALUES('ambiguous');
        """)
        connection.commit()
    finally:
        connection.close()

    report = inspect(cfg)

    assert report["status"] == "requires_owner_review"
    assert report["reviews"] == {"memory": 1, "telegram": 1, "total": 2}
    assert "test-token" not in repr(report)
    assert "test-secret" not in repr(report)


def test_pilot_doctor_blocks_missing_state_database(tmp_path):
    report = inspect(_configured(tmp_path))

    assert report["status"] == "blocked"
    assert report["state_integrity_ok"] is False


def test_pilot_doctor_blocks_when_any_autonomy_gate_is_enabled(tmp_path):
    cfg = _configured(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    sqlite3.connect(cfg.state_db).close()

    report = inspect(dataclasses.replace(cfg, autonomy_enabled=True))

    assert report["status"] == "blocked"
    assert report["autonomy_disabled"] is False
