from __future__ import annotations

import sqlite3

import pytest

from hive.core.operation_evidence import OperationEvidenceStore


def test_operation_evidence_records_only_safe_aggregate_data(tmp_path):
    store = OperationEvidenceStore(tmp_path / "evidence.sqlite", clock=lambda: 100.0)

    saved = store.record(
        operation="restore_drill", outcome="succeeded", metrics={"integrity_ok": True, "tables": 4}
    )

    assert saved.sequence == 1
    assert saved.recorded_at == 100.0
    assert saved.metrics == {"integrity_ok": True, "tables": 4}
    assert store.recent() == [saved]


def test_operation_evidence_rejects_text_payloads_and_unknown_labels(tmp_path):
    store = OperationEvidenceStore(tmp_path / "evidence.sqlite")

    with pytest.raises(ValueError, match="operation evidence type"):
        store.record(operation="telegram_message", outcome="succeeded")
    with pytest.raises(ValueError, match="metrics"):
        store.record(operation="state_backup", outcome="failed", metrics={"error": "secret text"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metric names"):
        store.record(operation="state_backup", outcome="failed", metrics={"user-id": 1})


def test_operation_evidence_table_rejects_update_or_delete(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = OperationEvidenceStore(path)
    store.record(operation="shadow_soak", outcome="started")

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE operation_evidence SET outcome='failed'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM operation_evidence")
    finally:
        connection.close()
