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
        _publish_without_overwrite(temporary_path, destination_path)
        return destination_path
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def restore_drill(source: str | Path, destination: str | Path) -> Path:
    """Restore ``source`` into a *new* drill database without any overwrite path.

    This is deliberately distinct from :func:`restore_backup`: it is for testing a
    backup, not for replacing live state.  The destination must not exist, and is
    published only after the copy and integrity check both succeed.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"backup does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("restore drill destination must differ from backup source")
    if destination_path.exists():
        raise FileExistsError(f"restore drill destination already exists: {destination_path}")
    ok, details = verify_database(source_path)
    if not ok:
        raise sqlite3.DatabaseError("refusing restore drill from invalid backup: " + "; ".join(details))

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
        restored_ok, restored_details = verify_database(temporary_path)
        if not restored_ok:
            raise sqlite3.DatabaseError(
                "restore drill database failed integrity check: " + "; ".join(restored_details)
            )
        _publish_without_overwrite(temporary_path, destination_path)
        return destination_path
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _publish_without_overwrite(temporary_path: Path, destination_path: Path) -> None:
    """Atomically publish a same-directory temporary file only if ``destination`` is absent.

    A hard link is used instead of ``os.replace`` so a concurrent process cannot
    turn a verified snapshot operation into an overwrite.  Both paths live in the
    same directory, making the link operation a single-filesystem operation.
    """
    os.link(temporary_path, destination_path)
    temporary_path.unlink()


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
