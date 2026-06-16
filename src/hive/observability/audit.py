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
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
                 max_rows: int = 10_000) -> None:
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
        self._max_rows = max_rows
        self._db.commit()

    def record(self, entry: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO audit_log(ts, tool, status, approved, error, args) VALUES(?,?,?,?,?,?)",
            (self._clock(), entry.get("tool", ""), entry.get("status", ""),
             1 if entry.get("approved") else 0, entry.get("error"),
             json.dumps(redact_args(entry.get("args", {})), default=str)),  # B2: redact secrets
        )
        self._db.commit()
        self.prune()

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, ts, tool, status, approved, error FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export(self, *, start_ts: float | None = None, end_ts: float | None = None,
               fmt: str = "json") -> list[dict]:
        """Return all audit entries in a date range as a list of dicts (JSON-serialisable)."""
        clauses, params = [], []
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(end_ts)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._db.execute(
            f"SELECT id, ts, tool, status, approved, error, args FROM audit_log {where} ORDER BY id",
            params,
        ).fetchall()
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def prune(self, max_rows: int | None = None) -> int:
        """Delete oldest entries beyond max_rows. Returns count deleted."""
        limit = max_rows if max_rows is not None else self._max_rows
        cur = self._db.execute(
            "DELETE FROM audit_log WHERE id NOT IN "
            "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
        if cur.rowcount:
            self._db.commit()
        return cur.rowcount

    def stats(self) -> dict:
        """Return audit summary grouped by tool and status (for dashboard/monitoring)."""
        by_tool = {}
        rows = self._db.execute(
            "SELECT tool, status, COUNT(*) AS n FROM audit_log GROUP BY tool, status"
        ).fetchall()
        for r in rows:
            tool_entry = by_tool.setdefault(r["tool"], {"total": 0, "by_status": {}})
            tool_entry["total"] += r["n"]
            tool_entry["by_status"][r["status"]] = r["n"]
        total_row = self._db.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
        return {"total": int(total_row["n"]), "by_tool": by_tool}

    def search(self, *, tool: str | None = None, status: str | None = None,
               limit: int = 50) -> list[dict]:
        """Search audit entries by tool name and/or status."""
        clauses, params = [], []
        if tool is not None:
            clauses.append("tool=?")
            params.append(tool)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(limit, 500))
        rows = self._db.execute(
            f"SELECT id, ts, tool, status, approved, error, args "
            f"FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def clear(self) -> None:
        """Remove all audit entries from the database."""
        self._db.execute("DELETE FROM audit_log")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
