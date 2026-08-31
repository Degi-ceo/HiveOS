"""Safe, explicit SQLite state backup and restore primitives.

These operations use SQLite's online backup API rather than filesystem copies, so a
WAL-mode source is captured as a single consistent snapshot.  They are intentionally
operator-triggered; no runtime or heartbeat path imports this module.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path


def verify_database(path: str | Path) -> tuple[bool, list[str]]:
    """Run SQLite integrity_check without creating or modifying ``path``."""
    db_path = Path(path)
    if not db_path.is_file():
        return False, [f"database does not exist: {db_path}"]
    conn: sqlite3.Connection | None = None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        return False, [str(exc)]
    finally:
        if conn is not None:
            conn.close()
    return rows == ["ok"], rows


def create_backup(source: str | Path, destination: str | Path) -> Path:
    """Create a verified consistent snapshot without overwriting a backup.

    The final destination is published only after SQLite completes the backup and
    its own integrity check succeeds.  A failed or interrupted operation leaves at
    most a uniquely named temporary file, never a plausible-looking final backup.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup destination must differ from source database")
    if destination_path.exists():
        raise FileExistsError(f"backup destination already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".partial", dir=destination_path.parent,
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        source_conn = sqlite3.connect(str(source_path))
        target_conn = sqlite3.connect(str(temporary_path))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        ok, details = verify_database(temporary_path)
        if not ok:
            raise sqlite3.DatabaseError("backup integrity check failed: " + "; ".join(details))
        os.replace(temporary_path, destination_path)
        return destination_path
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def restore_backup(source: str | Path, destination: str | Path, *, confirmed: bool = False) -> Path:
    """Restore a verified backup only after an explicit confirmation from the operator."""
    if not confirmed:
        raise PermissionError("restore requires explicit confirmation")
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"backup does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup source must differ from destination database")
    ok, details = verify_database(source_path)
    if not ok:
        raise sqlite3.DatabaseError("refusing restore from invalid backup: " + "; ".join(details))
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source_path))
    target_conn = sqlite3.connect(str(destination_path))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    restored_ok, restored_details = verify_database(destination_path)
    if not restored_ok:
        raise sqlite3.DatabaseError("restored database failed integrity check: " + "; ".join(restored_details))
    return destination_path
