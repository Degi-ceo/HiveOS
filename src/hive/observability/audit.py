"""
audit.py — SQLite audit log (the executor's audit sink).

The old Tools/registry.py appended JSONL to data/audit.log; SQLite-first (OpenClaw)
means the audit trail is a table, not a sidecar file. `record()` matches the
ToolExecutor audit-callback shape (a dict), so wiring is `ToolExecutor(..., audit=
audit_log.record)`. Depends on core only.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.redact import redact_args


class AuditLog:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")  # shared state DB: reduce writer lock contention
        self._clock = clock
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit_log("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tool TEXT, status TEXT, "
            "approved INTEGER, error TEXT, args TEXT)"
        )
        self._db.commit()

    def record(self, entry: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO audit_log(ts, tool, status, approved, error, args) VALUES(?,?,?,?,?,?)",
            (self._clock(), entry.get("tool", ""), entry.get("status", ""),
             1 if entry.get("approved") else 0, entry.get("error"),
             json.dumps(redact_args(entry.get("args", {})), default=str)),  # B2: redact secrets
        )
        self._db.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT tool, status, approved, error FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._db.close()
