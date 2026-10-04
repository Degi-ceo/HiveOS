"""Durable, bounded deployment-verification state; no probes or alert delivery."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PENDING = "pending"
VERIFYING = "verifying"
HEALTHY = "healthy"
DEGRADED = "degraded"

TARGETS = frozenset({"gateway", "orchestrator", "keeper"})
MODES = frozenset({"systemctl", "docker", "ssh"})
SIGNALS = frozenset({"doctor", "gateway", "smoke", "revision", "restart", "verifier", "timeout"})
MAX_SETTLING_SECONDS = 3600
MAX_LEASE_SECONDS = 3600
MAX_VERIFICATION_CLAIMS = 2

_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class DeployRecord:
    """Only identifiers, bounded state, and allowlisted failure codes are durable."""

    id: str
    run_id: str
    target: str
    mode: str
    host_key: str
    expected_sha: str
    baseline_sha: str
    status: str
    due_at: float
    created_at: float
    updated_at: float
    claim_count: int
    owner: str | None
    lease_until: float | None
    failed_signals: tuple[str, ...]
    completed_at: float | None
    alert_owner: str | None
    alert_lease_until: float | None
    alert_claim_count: int
    alert_sent_at: float | None
    incident_recorded_at: float | None
    restart_confirmed_at: float | None

    @property
    def state(self) -> str:
        return self.status

    @property
    def verdict(self) -> str | None:
        return self.status if self.status in {HEALTHY, DEGRADED} else None


class DeployLedger:
    """SQLite sidecar for deploy verdicts and leased verification/alert work.

    A separate connection is used per operation. Callers supply the host key
    they own when claiming verification. This class never runs a probe, restarts
    a service, or sends an alert.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        if not self._path or self._path == ":memory:":
            raise ValueError("a durable SQLite path is required")
        self._clock = clock
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS deploy_ledger (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    target TEXT NOT NULL CHECK (target IN ('gateway', 'orchestrator', 'keeper')),
                    mode TEXT NOT NULL CHECK (mode IN ('systemctl', 'docker', 'ssh')),
                    host_key TEXT NOT NULL,
                    expected_sha TEXT NOT NULL,
                    baseline_sha TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'verifying', 'healthy', 'degraded')),
                    due_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    claim_count INTEGER NOT NULL DEFAULT 0 CHECK (claim_count BETWEEN 0 AND 2),
                    owner TEXT,
                    lease_until REAL,
                    failed_signals TEXT NOT NULL DEFAULT '[]',
                    completed_at REAL,
                    alert_owner TEXT,
                    alert_lease_until REAL,
                    alert_claim_count INTEGER NOT NULL DEFAULT 0 CHECK (alert_claim_count >= 0),
                    alert_sent_at REAL,
                    incident_recorded_at REAL,
                    restart_confirmed_at REAL
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(deploy_ledger)")}
            if "alert_claim_count" not in columns:
                db.execute(
                    "ALTER TABLE deploy_ledger ADD COLUMN alert_claim_count INTEGER NOT NULL DEFAULT 0"
                )
            if "incident_recorded_at" not in columns:
                db.execute("ALTER TABLE deploy_ledger ADD COLUMN incident_recorded_at REAL")
            if "restart_confirmed_at" not in columns:
                db.execute("ALTER TABLE deploy_ledger ADD COLUMN restart_confirmed_at REAL")
                # The pre-M28 standalone ledger did not stage live restarts.
                db.execute("UPDATE deploy_ledger SET restart_confirmed_at=created_at")
            db.execute(
                "CREATE INDEX IF NOT EXISTS deploy_ledger_due ON deploy_ledger(host_key, status, due_at, lease_until)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS deploy_ledger_healthy "
                "ON deploy_ledger(host_key, target, mode, status, created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS deploy_ledger_alert "
                "ON deploy_ledger(status, alert_sent_at, alert_lease_until)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS deploy_ledger_incident "
                "ON deploy_ledger(host_key, status, incident_recorded_at, completed_at)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        finally:
            db.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _key(value: object, name: str) -> str:
        if not isinstance(value, str) or not _KEY.fullmatch(value):
            raise ValueError(f"{name} must be a bounded identifier")
        return value

    @staticmethod
    def _sha(value: object, *, optional: bool = False) -> str:
        if optional and value == "":
            return ""
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ValueError("SHA must be 40 or 64 hexadecimal characters")
        return value.lower()

    @staticmethod
    def _duration(value: object, name: str, maximum: float, *, allow_zero: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite bounded duration")
        try:
            duration = float(value)
        except OverflowError as exc:
            raise ValueError(f"{name} must be a finite bounded duration") from exc
        if not math.isfinite(duration) or duration > maximum or (duration < 0 if allow_zero else duration <= 0):
            raise ValueError(f"{name} must be a finite bounded duration")
        return duration

    def _now(self, now: float | None) -> float:
        value = self._clock() if now is None else now
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("now must be a finite timestamp")
        try:
            timestamp = float(value)
        except OverflowError as exc:
            raise ValueError("now must be a finite timestamp") from exc
        if not math.isfinite(timestamp):
            raise ValueError("now must be a finite timestamp")
        return timestamp

    @staticmethod
    def _signals(values: object) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)) or len(values) > len(SIGNALS):
            raise ValueError("failed_signals must contain allowlisted codes")
        if any(not isinstance(value, str) or value not in SIGNALS for value in values):
            raise ValueError("failed_signals must contain allowlisted codes")
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _record(row: sqlite3.Row) -> DeployRecord:
        return DeployRecord(
            id=row["id"],
            run_id=row["run_id"],
            target=row["target"],
            mode=row["mode"],
            host_key=row["host_key"],
            expected_sha=row["expected_sha"],
            baseline_sha=row["baseline_sha"],
            status=row["status"],
            due_at=row["due_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            claim_count=row["claim_count"],
            owner=row["owner"],
            lease_until=row["lease_until"],
            failed_signals=DeployLedger._signals(json.loads(row["failed_signals"])),
            completed_at=row["completed_at"],
            alert_owner=row["alert_owner"],
            alert_lease_until=row["alert_lease_until"],
            alert_claim_count=row["alert_claim_count"],
            alert_sent_at=row["alert_sent_at"],
            incident_recorded_at=row["incident_recorded_at"],
            restart_confirmed_at=row["restart_confirmed_at"],
        )

    def schedule(
        self,
        run_id: str,
        target: str,
        mode: str,
        host_key: str,
        expected_sha: str,
        baseline_sha: str = "",
        now: float | None = None,
        settling_seconds: float = 30,
        await_restart: bool = False,
    ) -> DeployRecord:
        run_id = self._key(run_id, "run_id")
        host_key = self._key(host_key, "host_key")
        if not isinstance(target, str) or target not in TARGETS or not isinstance(mode, str) or mode not in MODES:
            raise ValueError("unsupported deploy target or mode")
        expected_sha = self._sha(expected_sha)
        baseline_sha = self._sha(baseline_sha, optional=True)
        timestamp = self._now(now)
        settling = self._duration(settling_seconds, "settling_seconds", MAX_SETTLING_SECONDS, allow_zero=True)
        if not isinstance(await_restart, bool):
            raise ValueError("await_restart must be a boolean")
        due_at = timestamp + (60 if await_restart else settling)
        if not math.isfinite(due_at):
            raise ValueError("due_at must be finite")
        deploy_id = str(uuid.uuid4())
        with self._transaction() as db:
            db.execute(
                """INSERT INTO deploy_ledger
                   (id, run_id, target, mode, host_key, expected_sha, baseline_sha,
                    status, due_at, created_at, updated_at, restart_confirmed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    deploy_id,
                    run_id,
                    target,
                    mode,
                    host_key,
                    expected_sha,
                    baseline_sha,
                    PENDING,
                    due_at,
                    timestamp,
                    timestamp,
                    None if await_restart else timestamp,
                ),
            )
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (deploy_id,)).fetchone()
            return self._record(row)

    def confirm_restart(self, id: str, *, settling_seconds: float = 30,
                        now: float | None = None) -> DeployRecord:
        """Start settling only after the bounded restart command succeeds."""
        timestamp = self._now(now)
        settling = self._duration(settling_seconds, "settling_seconds",
                                  MAX_SETTLING_SECONDS, allow_zero=True)
        due_at = timestamp + settling
        if not math.isfinite(due_at):
            raise ValueError("due_at must be finite")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            if row is None:
                raise LookupError("deployment record not found")
            if (row["status"] != PENDING or row["restart_confirmed_at"] is not None
                    or row["due_at"] <= timestamp):
                raise ValueError("restart receipt is not awaiting confirmation")
            db.execute(
                "UPDATE deploy_ledger SET restart_confirmed_at=?, due_at=?, updated_at=? WHERE id=?",
                (timestamp, due_at, timestamp, id),
            )
            result = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            return self._record(result)

    def claim_due(
        self,
        host_key: str,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60,
    ) -> DeployRecord | None:
        host_key = self._key(host_key, "host_key")
        owner = self._key(owner, "owner")
        timestamp = self._now(now)
        lease = self._duration(lease_seconds, "lease_seconds", MAX_LEASE_SECONDS)
        if not math.isfinite(timestamp + lease):
            raise ValueError("lease expiry must be finite")
        with self._transaction() as db:
            self._expire_unconfirmed(db, timestamp, host_key=host_key)
            self._expire_exhausted(db, timestamp, host_key=host_key)
            row = db.execute(
                """SELECT * FROM deploy_ledger WHERE host_key=? AND due_at<=?
                   AND restart_confirmed_at IS NOT NULL
                   AND (status=? OR (status=? AND lease_until<=? AND claim_count<?))
                   ORDER BY due_at, created_at, rowid LIMIT 1""",
                (host_key, timestamp, PENDING, VERIFYING, timestamp, MAX_VERIFICATION_CLAIMS),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE deploy_ledger SET status=?, claim_count=claim_count+1,
                   owner=?, lease_until=?, updated_at=? WHERE id=?""",
                (VERIFYING, owner, timestamp + lease, timestamp, row["id"]),
            )
            claimed = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (row["id"],)).fetchone()
            return self._record(claimed)

    def finish(
        self,
        id: str,
        owner: str,
        verdict: str,
        failed_signals: tuple[str, ...] = (),
        now: float | None = None,
        *,
        claim_count: int,
    ) -> DeployRecord:
        owner = self._key(owner, "owner")
        if not isinstance(verdict, str) or verdict not in {HEALTHY, DEGRADED}:
            raise ValueError("verdict must be healthy or degraded")
        signals = self._signals(failed_signals)
        if (verdict == HEALTHY and signals) or (verdict == DEGRADED and not signals):
            raise ValueError("failed_signals do not match verdict")
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            if row is None:
                raise LookupError("deployment record not found")
            if (
                row["status"] != VERIFYING
                or row["owner"] != owner
                or row["lease_until"] <= timestamp
                or isinstance(claim_count, bool)
                or not isinstance(claim_count, int)
                or row["claim_count"] != claim_count
            ):
                raise ValueError("verification claim is not active for owner")
            db.execute(
                """UPDATE deploy_ledger SET status=?, failed_signals=?, completed_at=?,
                   lease_until=NULL, updated_at=? WHERE id=?""",
                (verdict, json.dumps(signals), timestamp, timestamp, id),
            )
            result = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            return self._record(result)

    def mark_restart_failed(self, id: str, now: float | None = None) -> DeployRecord:
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            if row is None:
                raise LookupError("deployment record not found")
            if row["status"] == DEGRADED:
                return self._record(row)
            if row["status"] == HEALTHY:
                raise ValueError("a healthy deployment cannot be marked restart-failed")
            db.execute(
                """UPDATE deploy_ledger SET status=?, failed_signals='["restart"]',
                   owner=NULL, lease_until=NULL, completed_at=?, updated_at=? WHERE id=?""",
                (DEGRADED, timestamp, timestamp, id),
            )
            result = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            return self._record(result)

    def get(self, id: str) -> DeployRecord | None:
        with self._reader() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
        return None if row is None else self._record(row)

    def last_healthy_sha(self, host_key: str, target: str, mode: str) -> str | None:
        host_key = self._key(host_key, "host_key")
        if not isinstance(target, str) or target not in TARGETS or not isinstance(mode, str) or mode not in MODES:
            raise ValueError("unsupported deploy target or mode")
        with self._reader() as db:
            row = db.execute(
                """SELECT expected_sha FROM deploy_ledger
                   WHERE host_key=? AND target=? AND mode=? AND status=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (host_key, target, mode, HEALTHY),
            ).fetchone()
        return None if row is None else str(row["expected_sha"])

    def next_degraded_without_incident(self, host_key: str, now: float | None = None) -> DeployRecord | None:
        """Return one local degraded receipt, including an exhausted lease."""
        host_key = self._key(host_key, "host_key")
        timestamp = self._now(now)
        with self._transaction() as db:
            self._expire_unconfirmed(db, timestamp, host_key=host_key)
            self._expire_exhausted(db, timestamp, host_key=host_key)
            row = db.execute(
                """SELECT * FROM deploy_ledger WHERE host_key=? AND status=?
                   AND incident_recorded_at IS NULL
                   ORDER BY completed_at, created_at, rowid LIMIT 1""",
                (host_key, DEGRADED),
            ).fetchone()
            return None if row is None else self._record(row)

    def mark_incident_recorded(self, id: str, host_key: str, now: float | None = None) -> DeployRecord:
        """Acknowledge only after the idempotent incident write succeeds."""
        host_key = self._key(host_key, "host_key")
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            if row is None:
                raise LookupError("deployment record not found")
            if row["host_key"] != host_key or row["status"] != DEGRADED:
                raise ValueError("degraded receipt is not owned by host")
            if row["incident_recorded_at"] is None:
                db.execute(
                    "UPDATE deploy_ledger SET incident_recorded_at=? WHERE id=?",
                    (timestamp, id),
                )
                row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            return self._record(row)

    @staticmethod
    def _expire_unconfirmed(db: sqlite3.Connection, now: float, *, host_key: str | None = None) -> None:
        host_filter = " AND host_key=?" if host_key is not None else ""
        params: tuple[object, ...] = (DEGRADED, now, now, PENDING, now)
        if host_key is not None:
            params += (host_key,)
        db.execute(
            """UPDATE deploy_ledger SET status=?, failed_signals='["restart"]',
               completed_at=?, updated_at=? WHERE status=?
               AND restart_confirmed_at IS NULL AND due_at<=?""" + host_filter,
            params,
        )

    @staticmethod
    def _expire_exhausted(db: sqlite3.Connection, now: float, *, host_key: str | None = None) -> None:
        host_filter = " AND host_key=?" if host_key is not None else ""
        params: tuple[object, ...] = (DEGRADED, now, now, VERIFYING, MAX_VERIFICATION_CLAIMS, now)
        if host_key is not None:
            params += (host_key,)
        db.execute(
            """UPDATE deploy_ledger SET status=?, owner=NULL, lease_until=NULL,
               failed_signals='["timeout"]', completed_at=?, updated_at=?
               WHERE status=? AND claim_count>=? AND lease_until<=?"""
            + host_filter,
            params,
        )

    def claim_alert(
        self,
        host_key: str,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60,
    ) -> DeployRecord | None:
        host_key = self._key(host_key, "host_key")
        owner = self._key(owner, "owner")
        timestamp = self._now(now)
        lease = self._duration(lease_seconds, "lease_seconds", MAX_LEASE_SECONDS)
        if not math.isfinite(timestamp + lease):
            raise ValueError("lease expiry must be finite")
        with self._transaction() as db:
            self._expire_unconfirmed(db, timestamp, host_key=host_key)
            self._expire_exhausted(db, timestamp, host_key=host_key)
            row = db.execute(
                """SELECT * FROM deploy_ledger WHERE host_key=? AND status=?
                   AND alert_sent_at IS NULL
                   AND (alert_lease_until IS NULL OR alert_lease_until<=?)
                   ORDER BY completed_at, created_at, rowid LIMIT 1""",
                (host_key, DEGRADED, timestamp),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE deploy_ledger SET alert_owner=?, alert_lease_until=?, "
                "alert_claim_count=alert_claim_count+1 WHERE id=?",
                (owner, timestamp + lease, row["id"]),
            )
            claimed = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (row["id"],)).fetchone()
            return self._record(claimed)

    def mark_alert_sent(
        self, id: str, owner: str, now: float | None = None, *, claim_count: int,
    ) -> DeployRecord:
        owner = self._key(owner, "owner")
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            if row is None:
                raise LookupError("deployment record not found")
            if (
                isinstance(claim_count, bool)
                or not isinstance(claim_count, int)
                or row["alert_claim_count"] != claim_count
            ):
                raise ValueError("alert claim generation is stale")
            if row["alert_sent_at"] is not None and row["alert_owner"] == owner:
                return self._record(row)
            if (
                row["status"] != DEGRADED
                or row["alert_owner"] != owner
                or row["alert_lease_until"] is None
                or row["alert_lease_until"] <= timestamp
            ):
                raise ValueError("alert claim is not active for owner")
            db.execute(
                "UPDATE deploy_ledger SET alert_sent_at=?, alert_lease_until=NULL WHERE id=?",
                (timestamp, id),
            )
            result = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (id,)).fetchone()
            return self._record(result)
