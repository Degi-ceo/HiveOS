from __future__ import annotations

import sqlite3

import pytest

from hive.autonomy.shadow import run_shadow
from hive.autonomy.tasks import TaskBoard
from hive.surfaces import cli


def test_shadow_reads_task_inventory_without_mutating_source(tmp_path):
    source = tmp_path / "state.sqlite"
    board = TaskBoard(source, clock=lambda: 100.0)
    board.enqueue("tool", {"name": "safe"}, scheduled_for=90.0)
    board.enqueue("tool", {"name": "later"}, scheduled_for=110.0)
    board._db.close()
    before = source.read_bytes()

    report = run_shadow(source, tmp_path / "evidence.sqlite", now=100.0)

    assert report.integrity_ok is True
    assert report.task_states == {"pending": 2}
    assert report.due_tasks == 1
    assert source.read_bytes() == before
    evidence = sqlite3.connect(str(tmp_path / "evidence.sqlite"))
    try:
        assert evidence.execute("SELECT COUNT(*) FROM hive_shadow_runs").fetchone()[0] == 1
    finally:
        evidence.close()


def test_shadow_handles_an_empty_schema_without_creating_tables(tmp_path):
    source = tmp_path / "empty.sqlite"
    sqlite3.connect(str(source)).close()

    report = run_shadow(source, tmp_path / "evidence.sqlite", now=100.0)

    assert report.task_states == {}
    assert report.due_tasks == 0
    assert "hive_tasks table is absent" in report.notes[0]
    source_conn = sqlite3.connect(str(source))
    try:
        assert source_conn.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='table'").fetchone()[0] == 0
    finally:
        source_conn.close()


def test_shadow_cli_helper_records_evidence_without_constructing_runtime(tmp_path, capsys):
    source = tmp_path / "state.sqlite"
    TaskBoard(source)._db.close()
    evidence = tmp_path / "evidence.sqlite"

    assert cli._shadow(str(source), str(evidence)) == 0

    assert "no tools or external channels invoked" in capsys.readouterr().out
    assert evidence.is_file()


def test_shadow_refuses_the_source_as_its_evidence_destination(tmp_path):
    source = tmp_path / "state.sqlite"
    sqlite3.connect(str(source)).close()

    with pytest.raises(ValueError, match="must differ"):
        run_shadow(source, source)


def test_shadow_refuses_an_invalid_source_database(tmp_path):
    source = tmp_path / "invalid.sqlite"
    source.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError, match="invalid source"):
        run_shadow(source, tmp_path / "evidence.sqlite")
