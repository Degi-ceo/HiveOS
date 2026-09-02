from __future__ import annotations

import sqlite3

import pytest

from hive.core.sqlite_ops import create_backup, restore_backup, restore_drill, verify_database


def _seed(path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS records(value TEXT NOT NULL)")
        conn.execute("INSERT INTO records(value) VALUES(?)", (value,))


def _values(path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT value FROM records ORDER BY rowid")]


def test_create_backup_uses_verified_consistent_snapshot(tmp_path):
    source = tmp_path / "state.sqlite"
    backup = tmp_path / "backups" / "snapshot.sqlite"
    _seed(source, "first")
    _seed(source, "second")

    saved = create_backup(source, backup)

    assert saved == backup
    assert verify_database(backup) == (True, ["ok"])
    assert _values(backup) == ["first", "second"]
    assert not list(backup.parent.glob("*.partial"))


def test_create_backup_refuses_existing_destination(tmp_path):
    source = tmp_path / "state.sqlite"
    backup = tmp_path / "snapshot.sqlite"
    _seed(source, "source")
    _seed(backup, "existing")

    with pytest.raises(FileExistsError):
        create_backup(source, backup)

    assert _values(backup) == ["existing"]


def test_restore_requires_confirmation_and_verifies_both_sides(tmp_path):
    source = tmp_path / "source.sqlite"
    backup = tmp_path / "snapshot.sqlite"
    target = tmp_path / "target.sqlite"
    _seed(source, "authoritative")
    create_backup(source, backup)
    _seed(target, "stale")

    with pytest.raises(PermissionError):
        restore_backup(backup, target)
    assert _values(target) == ["stale"]

    restored = restore_backup(backup, target, confirmed=True)
    assert restored == target
    assert verify_database(target) == (True, ["ok"])
    assert _values(target) == ["authoritative"]


def test_restore_drill_never_overwrites_an_existing_destination(tmp_path):
    source = tmp_path / "source.sqlite"
    backup = tmp_path / "snapshot.sqlite"
    drill = tmp_path / "drills" / "restored.sqlite"
    _seed(source, "authoritative")
    create_backup(source, backup)

    restored = restore_drill(backup, drill)

    assert restored == drill
    assert verify_database(drill) == (True, ["ok"])
    assert _values(drill) == ["authoritative"]
    with pytest.raises(FileExistsError):
        restore_drill(backup, drill)
    assert _values(drill) == ["authoritative"]
    assert not list(drill.parent.glob("*.partial"))


def test_verify_database_fails_closed_for_missing_or_invalid_file(tmp_path):
    missing_ok, missing_details = verify_database(tmp_path / "missing.sqlite")
    assert missing_ok is False
    assert "does not exist" in missing_details[0]

    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not sqlite")
    invalid_ok, invalid_details = verify_database(invalid)
    assert invalid_ok is False
    assert invalid_details
