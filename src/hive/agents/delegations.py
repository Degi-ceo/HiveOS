"""Durable, fenced lifecycle records for delegated specialist work."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hive.agents.profiles import specialist_profile
from hive.core.process import process_is_alive
from hive.core.redact import redact_value

QUEUED, RUNNING, REVIEW_REQUIRED, COMPLETED, FAILED, CANCELLED = (
    "queued", "running", "review_required", "completed", "failed", "cancelled",
)


@dataclass(frozen=True, slots=True)
class DelegationRecord:
    id: str
    parent_run_id: str
    child_run_id: str
    role: str
    state: str
    attempts: int
    max_attempts: int
    created_ts: float
    updated_ts: float
    safe_summary: str = ""
    owner_host: str = ""
    owner_pid: int = 0


class DelegationLedger:
    """SQLite state machine which stores no raw task prompt or worker output."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
                 process_id: int | None = None, hostname: str | None = None,
                 machine_identity: str | None = None,
                 process_is_alive: Callable[[int], bool] = process_is_alive) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout=5000")
        self._configure_journal_mode()
        self._clock = clock
        self._process_id = os.getpid() if process_id is None else int(process_id)
        self._hostname = socket.gethostname() if hostname is None else str(hostname)
        self._machine_identity = machine_identity or _default_machine_identity()
        self._owner_instance_id = uuid.uuid4().hex
        self._process_is_alive = process_is_alive
        self._initialize_schema()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO hive_delegation_owners("
                "owner_instance_id, owner_machine_id, owner_host, owner_pid, registered_ts"
                ") VALUES(?,?,?,?,?)",
                (self._owner_instance_id, self._machine_identity, self._hostname,
                 self._process_id, self._clock()),
            )

    def _configure_journal_mode(self) -> None:
        """Enable WAL without turning a simultaneous cold start into a hard failure."""
        for attempt in range(3):
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                transient = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not transient or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _initialize_schema(self) -> None:
        """Run additive schema changes under one SQLite writer transaction."""
        for attempt in range(3):
            try:
                with self._lock:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegations("
                        "id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL, child_run_id TEXT NOT NULL, "
                        "role TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                        "max_attempts INTEGER NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL, "
                        "safe_summary TEXT NOT NULL DEFAULT '', owner_host TEXT NOT NULL DEFAULT '', "
                        "owner_pid INTEGER NOT NULL DEFAULT 0, owner_machine_id TEXT NOT NULL DEFAULT '', "
                        "owner_instance_id TEXT NOT NULL DEFAULT '')"
                    )
                    self._db.execute(
                        "CREATE INDEX IF NOT EXISTS hive_delegations_parent "
                        "ON hive_delegations(parent_run_id, created_ts)"
                    )
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegation_events("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, delegation_id TEXT NOT NULL, "
                        "ts REAL NOT NULL, event_type TEXT NOT NULL, data_json TEXT NOT NULL)"
                    )
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegation_owners("
                        "owner_instance_id TEXT PRIMARY KEY, owner_machine_id TEXT NOT NULL, "
                        "owner_host TEXT NOT NULL, owner_pid INTEGER NOT NULL, registered_ts REAL NOT NULL)"
                    )
                    columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(hive_delegations)")}
                    for name, definition in (
                        ("owner_host", "TEXT NOT NULL DEFAULT ''"),
                        ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                        ("owner_machine_id", "TEXT NOT NULL DEFAULT ''"),
                        ("owner_instance_id", "TEXT NOT NULL DEFAULT ''"),
                    ):
                        if name not in columns:
                            self._db.execute(f"ALTER TABLE hive_delegations ADD COLUMN {name} {definition}")
                    self._db.commit()
                    return
            except sqlite3.OperationalError as exc:
                self._db.rollback()
                transient = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not transient or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def create(self, *, parent_run_id: str, child_run_id: str, role: str) -> DelegationRecord:
        profile = specialist_profile(role)
        now = self._clock()
        delegation_id = str(uuid.uuid4())
        with self._lock:
            self._db.execute(
                "INSERT INTO hive_delegations("
                "id, parent_run_id, child_run_id, role, state, attempts, max_attempts, created_ts, updated_ts, safe_summary"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (delegation_id, str(parent_run_id), str(child_run_id), profile.name, QUEUED,
                 0, profile.max_attempts, now, now, ""),
            )
            self._event(delegation_id, "queued", {"role": profile.name})
            self._db.commit()
        return self.get(delegation_id)  # type: ignore[return-value]

    def claim(self, delegation_id: str) -> int | None:
        now = self._clock()
        with self._lock:
            cur = self._db.execute(
                "UPDATE hive_delegations SET state=?, attempts=attempts+1, updated_ts=?, owner_host=?, owner_pid=?, "
                "owner_machine_id=?, owner_instance_id=? "
                "WHERE id=? AND state=? AND attempts < max_attempts",
                (RUNNING, now, self._hostname, self._process_id, self._machine_identity,
                 self._owner_instance_id, str(delegation_id), QUEUED),
            )
            if cur.rowcount != 1:
                self._db.commit()
                return None
            row = self._db.execute("SELECT attempts FROM hive_delegations WHERE id=?", (str(delegation_id),)).fetchone()
            attempt = int(row["attempts"])
            self._event(str(delegation_id), "started", {"attempt": attempt})
            self._db.commit()
            return attempt

    def finish(self, delegation_id: str, *, attempt: int, success: bool, summary: str = "",
               require_review: bool = False) -> bool:
        state = REVIEW_REQUIRED if success and require_review else COMPLETED if success else FAILED
        safe = str(redact_value(summary))[:500]
        with self._lock:
            cur = self._db.execute(
                "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                "owner_machine_id='', owner_instance_id='' "
                "WHERE id=? AND state=? AND attempts=?",
                (state, self._clock(), safe, str(delegation_id), RUNNING, int(attempt)),
            )
            if cur.rowcount:
                self._event(str(delegation_id), state, {"summary": safe})
            self._db.commit()
            return cur.rowcount == 1

    def cancel(self, delegation_id: str, *, attempt: int, summary: str = "cancelled") -> bool:
        """Record a cancellation only for the attempt currently holding the claim."""
        safe = str(redact_value(summary))[:500]
        with self._lock:
            cur = self._db.execute(
                "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                "owner_machine_id='', owner_instance_id='' "
                "WHERE id=? AND state=? AND attempts=?",
                (CANCELLED, self._clock(), safe, str(delegation_id), RUNNING, int(attempt)),
            )
            if cur.rowcount:
                self._event(str(delegation_id), CANCELLED, {"summary": safe})
            self._db.commit()
            return cur.rowcount == 1

    def get(self, delegation_id: str) -> DelegationRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM hive_delegations WHERE id=?", (str(delegation_id),)).fetchone()
            return _record(row) if row is not None else None

    def for_parent(self, parent_run_id: str, *, limit: int = 100) -> list[DelegationRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hive_delegations WHERE parent_run_id=? ORDER BY created_ts LIMIT ?",
                (str(parent_run_id), max(1, min(int(limit), 500))),
            ).fetchall()
            return [_record(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Mark only proven interrupted, locally owned worker attempts as failed.

        Delegation inputs and worker output are intentionally never persisted, so
        this method cannot safely replay a worker.  It only converts a proven
        local interruption into durable, redacted failure evidence.  Remote,
        live, and legacy unowned records are left untouched.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, attempts, owner_pid, owner_instance_id FROM hive_delegations "
                "WHERE state=? AND owner_machine_id=? AND owner_instance_id<>''",
                (RUNNING, self._machine_identity),
            ).fetchall()
            recovered = 0
            summary = "worker interrupted; recovery requires replanning"
            for row in rows:
                owner_pid = int(row["owner_pid"])
                owner_instance_id = str(row["owner_instance_id"])
                if owner_instance_id == self._owner_instance_id:
                    continue
                if self._process_is_alive(owner_pid):
                    active_owner = self._active_owner_instance(owner_pid)
                    if active_owner in (None, owner_instance_id):
                        continue
                cur = self._db.execute(
                    "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                    "owner_machine_id='', owner_instance_id='' WHERE id=? AND state=? AND attempts=? "
                    "AND owner_machine_id=? AND owner_pid=? AND owner_instance_id=?",
                    (FAILED, self._clock(), summary, str(row["id"]), RUNNING, int(row["attempts"]),
                     self._machine_identity, owner_pid, owner_instance_id),
                )
                if cur.rowcount:
                    self._event(str(row["id"]), "interrupted", {"summary": summary})
                    recovered += 1
            self._db.commit()
            return recovered

    def close(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM hive_delegation_owners WHERE owner_instance_id=?",
                             (self._owner_instance_id,))
            self._db.commit()
            self._db.close()

    def _active_owner_instance(self, owner_pid: int) -> str | None:
        row = self._db.execute(
            "SELECT owner_instance_id FROM hive_delegation_owners "
            "WHERE owner_machine_id=? AND owner_pid=? ORDER BY registered_ts DESC LIMIT 1",
            (self._machine_identity, owner_pid),
        ).fetchone()
        return str(row["owner_instance_id"]) if row is not None else None

    def _event(self, delegation_id: str, event_type: str, data: dict) -> None:
        self._db.execute(
            "INSERT INTO hive_delegation_events(delegation_id, ts, event_type, data_json) VALUES(?,?,?,?)",
            (delegation_id, self._clock(), event_type, json.dumps(redact_value(data), sort_keys=True)),
        )


def _record(row: sqlite3.Row) -> DelegationRecord:
    return DelegationRecord(
        id=str(row["id"]), parent_run_id=str(row["parent_run_id"]), child_run_id=str(row["child_run_id"]),
        role=str(row["role"]), state=str(row["state"]), attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]), created_ts=float(row["created_ts"]),
        updated_ts=float(row["updated_ts"]), safe_summary=str(row["safe_summary"]),
        owner_host=str(row["owner_host"]), owner_pid=int(row["owner_pid"]),
    )


def _default_machine_identity() -> str:
    """Return a stable, non-secret local-machine discriminator for SQLite ownership."""
    raw = f"{socket.gethostname()}:{uuid.getnode():012x}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
