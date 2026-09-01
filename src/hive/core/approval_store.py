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
KILLED = "killed"

EXECUTION_PENDING = "pending"
EXECUTION_IN_PROGRESS = "in_progress"
EXECUTION_SUCCEEDED = "succeeded"
EXECUTION_REQUIRES_REVIEW = "requires_review"


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
    execution_state: str = EXECUTION_PENDING
    execution_started_ts: float | None = None
    execution_finished_ts: float | None = None
    execution_error: str | None = None


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
          requested_ts REAL NOT NULL, decided_ts REAL, decided_by TEXT,
          execution_state TEXT NOT NULL DEFAULT 'pending', execution_started_ts REAL,
          execution_finished_ts REAL, execution_error TEXT)""")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(approval_snapshots)")}
        if "execution_state" not in columns:
            self._db.execute("ALTER TABLE approval_snapshots ADD COLUMN execution_state TEXT NOT NULL DEFAULT 'pending'")
        if "execution_started_ts" not in columns:
            self._db.execute("ALTER TABLE approval_snapshots ADD COLUMN execution_started_ts REAL")
        if "execution_finished_ts" not in columns:
            self._db.execute("ALTER TABLE approval_snapshots ADD COLUMN execution_finished_ts REAL")
        if "execution_error" not in columns:
            self._db.execute("ALTER TABLE approval_snapshots ADD COLUMN execution_error TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS approval_execution_state ON approval_snapshots(execution_state)")
        self._db.commit()

    def record_pending(self, approval_id: str, *, tool: str, args: dict,
                       reason: str, kind: str) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO approval_snapshots(approval_id, tool, args_json, reason, kind, state, requested_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (approval_id, tool, json.dumps(args, sort_keys=True), reason, kind, PENDING, self._clock()),
        )
        self._db.commit()
        return cur.rowcount == 1

    def decide(self, approval_id: str, *, approved: bool, decided_by: str) -> bool:
        state = APPROVED if approved else REJECTED
        return self._transition(approval_id, state=state, decided_by=decided_by)

    def expire(self, approval_id: str, *, decided_by: str = "system:expire") -> bool:
        """Persist an automatic TTL rejection without ever approving an action."""
        return self._transition(approval_id, state=EXPIRED, decided_by=decided_by)

    def kill(self, approval_id: str, *, decided_by: str = "system:kill") -> bool:
        """Persist an emergency-stop rejection without ever approving an action."""
        return self._transition(approval_id, state=KILLED, decided_by=decided_by)

    def _transition(self, approval_id: str, *, state: str, decided_by: str) -> bool:
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
            decided_ts=row["decided_ts"], decided_by=row["decided_by"],
            execution_state=row["execution_state"],
            execution_started_ts=row["execution_started_ts"],
            execution_finished_ts=row["execution_finished_ts"],
            execution_error=row["execution_error"])

    def begin_execution(self, approval_id: str) -> bool:
        """Durably record intent before a protected tool is invoked.

        Only one process can move an approved request into execution. A restart
        must quarantine this state instead of calling the tool a second time.
        """
        cur = self._db.execute(
            "UPDATE approval_snapshots SET execution_state=?, execution_started_ts=?, "
            "execution_finished_ts=NULL, execution_error=NULL "
            "WHERE approval_id=? AND state=? AND execution_state=?",
            (EXECUTION_IN_PROGRESS, self._clock(), approval_id, APPROVED, EXECUTION_PENDING),
        )
        self._db.commit()
        return cur.rowcount == 1

    def finish_execution(self, approval_id: str, *, succeeded: bool,
                         error: str | None = None) -> bool:
        """Persist a confirmed result, or quarantine an unconfirmed one."""
        execution_state = EXECUTION_SUCCEEDED if succeeded else EXECUTION_REQUIRES_REVIEW
        cur = self._db.execute(
            "UPDATE approval_snapshots SET execution_state=?, execution_finished_ts=?, execution_error=? "
            "WHERE approval_id=? AND execution_state=?",
            (execution_state, self._clock(), None if succeeded else (error or "unconfirmed execution")[:500],
             approval_id, EXECUTION_IN_PROGRESS),
        )
        self._db.commit()
        return cur.rowcount == 1

    def recover_executions(self) -> list[StoredApproval]:
        """Quarantine incomplete execution attempts and return terminal records.

        This method is intentionally reconciliation-only: it never invokes a
        tool. A crash after the durable pre-invocation marker is ambiguous, so
        the only safe recovery outcome is operator review.
        """
        now = self._clock()
        self._db.execute(
            "UPDATE approval_snapshots SET execution_state=?, execution_finished_ts=?, "
            "execution_error=COALESCE(execution_error, ?) WHERE execution_state=?",
            (EXECUTION_REQUIRES_REVIEW, now, "execution interrupted before a confirmed result",
             EXECUTION_IN_PROGRESS),
        )
        self._db.commit()
        rows = self._db.execute(
            "SELECT approval_id FROM approval_snapshots WHERE execution_state IN (?, ?)",
            (EXECUTION_SUCCEEDED, EXECUTION_REQUIRES_REVIEW),
        ).fetchall()
        return [self.get(row["approval_id"]) for row in rows]

    def quarantine_approved_unstarted(self) -> list[StoredApproval]:
        """Fail closed when a restart loses the live approval-gate handoff.

        An ``approved`` snapshot whose execution marker is still pending may
        have crashed before the gate handoff or before the protected invocation.
        The in-memory gate cannot be reconstructed safely, so it is never
        executed or retried automatically.
        """
        now = self._clock()
        self._db.execute(
            "UPDATE approval_snapshots SET execution_state=?, execution_finished_ts=?, "
            "execution_error=COALESCE(execution_error, ?) "
            "WHERE state=? AND execution_state=?",
            (EXECUTION_REQUIRES_REVIEW, now,
             "approved handoff was interrupted; action requires review",
             APPROVED, EXECUTION_PENDING),
        )
        self._db.commit()
        rows = self._db.execute(
            "SELECT approval_id FROM approval_snapshots WHERE state=? AND execution_state=?",
            (APPROVED, EXECUTION_REQUIRES_REVIEW),
        ).fetchall()
        return [self.get(row["approval_id"]) for row in rows]
    def pending(self) -> list[StoredApproval]:
        rows = self._db.execute("SELECT * FROM approval_snapshots WHERE state=? ORDER BY requested_ts", (PENDING,))
        return [self.get(row["approval_id"]) for row in rows]

    def close(self) -> None:
        self._db.close()
