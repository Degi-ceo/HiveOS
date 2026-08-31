"""Durable, fail-closed snapshots for protected approval requests."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class StoredApproval:
    approval_id: str
    tool: str
    args: dict
    reason: str
    kind: str
    state: str
    requested_ts: float
    decided_ts: float | None
    decided_by: str | None


class ApprovalStore:
    """SQLite source for request snapshots and human decisions, never execution."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.execute("""CREATE TABLE IF NOT EXISTS approval_snapshots(
          approval_id TEXT PRIMARY KEY, tool TEXT NOT NULL, args_json TEXT NOT NULL,
          reason TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
          requested_ts REAL NOT NULL, decided_ts REAL, decided_by TEXT)""")
        self._db.commit()

    def record_pending(self, approval_id: str, *, tool: str, args: dict,
                       reason: str, kind: str) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO approval_snapshots VALUES(?,?,?,?,?,?,?,NULL,NULL)",
            (approval_id, tool, json.dumps(args, sort_keys=True), reason, kind, PENDING, self._clock()),
        )
        self._db.commit()
        return cur.rowcount == 1

    def decide(self, approval_id: str, *, approved: bool, decided_by: str) -> bool:
        state = APPROVED if approved else REJECTED
        cur = self._db.execute(
            "UPDATE approval_snapshots SET state=?,decided_ts=?,decided_by=? "
            "WHERE approval_id=? AND state=?",
            (state, self._clock(), decided_by, approval_id, PENDING),
        )
        self._db.commit()
        return cur.rowcount == 1

    def expire_before(self, cutoff: float) -> int:
        cur = self._db.execute(
            "UPDATE approval_snapshots SET state=?,decided_ts=?,decided_by='system:expire' "
            "WHERE state=? AND requested_ts<?",
            (EXPIRED, self._clock(), PENDING, cutoff),
        )
        self._db.commit()
        return cur.rowcount

    def get(self, approval_id: str) -> StoredApproval | None:
        row = self._db.execute("SELECT * FROM approval_snapshots WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            return None
        return StoredApproval(approval_id=row["approval_id"], tool=row["tool"],
            args=json.loads(row["args_json"]), reason=row["reason"], kind=row["kind"],
            state=row["state"], requested_ts=row["requested_ts"],
            decided_ts=row["decided_ts"], decided_by=row["decided_by"])

    def pending(self) -> list[StoredApproval]:
        rows = self._db.execute("SELECT * FROM approval_snapshots WHERE state=? ORDER BY requested_ts", (PENDING,))
        return [self.get(row["approval_id"]) for row in rows]

    def close(self) -> None:
        self._db.close()
