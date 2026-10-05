"""Fail-closed, same-revision recovery for a local managed gateway.

This module deliberately does not implement a code rollback.  A deployment
receipt describes a source revision but HiveOS currently has no immutable
installed-release selector, so changing a checkout, branch, remote ref, or
service unit would not be a trustworthy recovery action.  The only action
available here is one explicitly enabled ``systemctl restart`` after proving
that the current checkout is already the same revision as the recorded healthy
baseline.

The intent is durable before the restart command.  A process crash between the
intent and its terminal observation is therefore *uncertain*, never retried,
and latches autonomous task dispatch until an operator investigates.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger, DeployRecord
from hive.core.revision import detect_source_revision
from hive.core.safety_state import SafetyStateStore
from hive.core.service_identity import is_managed_gateway_process

PREPARED = "prepared"
STAGED = "staged"
AWAITING_VERDICT = "awaiting_verdict"
RESOLVED = "resolved"
FAILED = "failed"
UNCERTAIN = "uncertain"
SUPPRESSED = "suppressed"
_HALT_NAME = "deployment_recovery"
_HALTING_STATES = frozenset({FAILED, UNCERTAIN, SUPPRESSED})
_MAX_RECOVERY_DEADLINE_SECONDS = 7200.0


@dataclass(frozen=True, slots=True)
class DeploymentRecoveryRecord:
    """Safe recovery metadata; it contains no command output or credentials."""

    source_receipt_id: str
    recovery_receipt_id: str
    host_key: str
    run_id: str
    expected_sha: str
    state: str
    created_at: float
    updated_at: float
    completed_at: float | None
    deadline_at: float
    incident_recorded_at: float | None


class DeploymentRecoveryLedger:
    """One durable, non-repeatable recovery intent per degraded receipt."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        if not self._path or self._path == ":memory:":
            raise ValueError("a durable SQLite path is required")
        self._clock = clock
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS deployment_recovery_ledger (
                    source_receipt_id TEXT PRIMARY KEY,
                    recovery_receipt_id TEXT NOT NULL DEFAULT '',
                    host_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    expected_sha TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'prepared', 'staged', 'awaiting_verdict', 'resolved',
                        'failed', 'uncertain', 'suppressed'
                    )),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    deadline_at REAL NOT NULL DEFAULT 0,
                    incident_recorded_at REAL
                )
                """
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS autonomy_latches(
                    name TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    engaged_at REAL NOT NULL
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(deployment_recovery_ledger)")}
            if "deadline_at" not in columns:
                db.execute(
                    "ALTER TABLE deployment_recovery_ledger "
                    "ADD COLUMN deadline_at REAL NOT NULL DEFAULT 0"
                )
            if "incident_recorded_at" not in columns:
                db.execute(
                    "ALTER TABLE deployment_recovery_ledger ADD COLUMN incident_recorded_at REAL"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS deployment_recovery_active "
                "ON deployment_recovery_ledger(host_key, state, created_at)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS deployment_recovery_receipt "
                "ON deployment_recovery_ledger(recovery_receipt_id) "
                "WHERE recovery_receipt_id<>''"
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

    def _now(self, now: float | None) -> float:
        value = self._clock() if now is None else now
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("now must be a finite timestamp")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("now must be a finite timestamp")
        return timestamp

    @staticmethod
    def _record(row: sqlite3.Row) -> DeploymentRecoveryRecord:
        return DeploymentRecoveryRecord(
            source_receipt_id=str(row["source_receipt_id"]),
            recovery_receipt_id=str(row["recovery_receipt_id"]),
            host_key=str(row["host_key"]),
            run_id=str(row["run_id"]),
            expected_sha=str(row["expected_sha"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            completed_at=(None if row["completed_at"] is None else float(row["completed_at"])),
            deadline_at=float(row["deadline_at"]),
            incident_recorded_at=(
                None if row["incident_recorded_at"] is None else float(row["incident_recorded_at"])
            ),
        )

    @staticmethod
    def _latch(db: sqlite3.Connection, source_receipt_id: str, reason: str, timestamp: float) -> None:
        db.execute(
            "INSERT OR IGNORE INTO autonomy_latches(name, reason, engaged_at) VALUES (?, ?, ?)",
            (_HALT_NAME, f"{reason}:{source_receipt_id}", timestamp),
        )

    def get(self, source_receipt_id: str) -> DeploymentRecoveryRecord | None:
        with self._reader() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def begin(self, source: DeployRecord, *, now: float | None = None) -> DeploymentRecoveryRecord | None:
        """Persist the sole permitted action before scheduling or running it."""
        if source.status != DEGRADED or source.target != "gateway" or source.mode != "systemctl":
            raise ValueError("recovery source must be a degraded gateway/systemctl receipt")
        timestamp = self._now(now)
        with self._transaction() as db:
            inserted = db.execute(
                """INSERT OR IGNORE INTO deployment_recovery_ledger
                   (source_receipt_id, host_key, run_id, expected_sha, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source.id, source.host_key, source.run_id, source.expected_sha,
                 PREPARED, timestamp, timestamp),
            ).rowcount
            if not inserted:
                return None
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source.id,),
            ).fetchone()
            return self._record(row)

    def stage(
        self, source_receipt_id: str, recovery_receipt_id: str, *,
        deadline_seconds: float, now: float | None = None,
    ) -> DeploymentRecoveryRecord:
        if not recovery_receipt_id:
            raise ValueError("recovery receipt id is required")
        timestamp = self._now(now)
        if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
                or not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= _MAX_RECOVERY_DEADLINE_SECONDS):
            raise ValueError("recovery deadline must be finite and bounded")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone()
            if row is None or row["state"] != PREPARED:
                raise ValueError("recovery intent is not prepared")
            db.execute(
                "UPDATE deployment_recovery_ledger SET recovery_receipt_id=?, state=?, deadline_at=?, updated_at=? "
                "WHERE source_receipt_id=?",
                (recovery_receipt_id, STAGED, timestamp + float(deadline_seconds), timestamp, source_receipt_id),
            )
            return self._record(db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone())

    def mark_command(self, source_receipt_id: str, *, success: bool, now: float | None = None) -> DeploymentRecoveryRecord:
        timestamp = self._now(now)
        next_state = AWAITING_VERDICT if success else FAILED
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone()
            if row is None or row["state"] != STAGED:
                raise ValueError("recovery intent is not staged")
            db.execute(
                "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                "WHERE source_receipt_id=?",
                (next_state, timestamp, None if success else timestamp, source_receipt_id),
            )
            if not success:
                self._latch(db, source_receipt_id, "restart_failed", timestamp)
            return self._record(db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone())

    def finish_verdict(self, recovery_receipt_id: str, verdict: str, *, now: float | None = None) -> DeploymentRecoveryRecord | None:
        if verdict not in {HEALTHY, DEGRADED}:
            raise ValueError("recovery verdict must be healthy or degraded")
        timestamp = self._now(now)
        next_state = RESOLVED if verdict == HEALTHY else FAILED
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE recovery_receipt_id=?",
                (recovery_receipt_id,),
            ).fetchone()
            if row is None or row["state"] != AWAITING_VERDICT:
                return None
            db.execute(
                "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                "WHERE source_receipt_id=?",
                (next_state, timestamp, timestamp, row["source_receipt_id"]),
            )
            if verdict == DEGRADED:
                self._latch(db, str(row["source_receipt_id"]), "failed_verification", timestamp)
            return self._record(db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (row["source_receipt_id"],),
            ).fetchone())

    def reconcile_startup(self, host_key: str, *, now: float | None = None) -> tuple[DeploymentRecoveryRecord, ...]:
        """Fence a pre-stage crash; linked receipts are reconciled before expiry."""
        timestamp = self._now(now)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT source_receipt_id FROM deployment_recovery_ledger "
                "WHERE host_key=? AND state='prepared'",
                (host_key,),
            ).fetchall()
            if rows:
                db.execute(
                    "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                    "WHERE host_key=? AND state='prepared'",
                    (UNCERTAIN, timestamp, timestamp, host_key),
                )
                self._latch(db, str(rows[0]["source_receipt_id"]), "interrupted", timestamp)
            stale = db.execute(
                "SELECT source_receipt_id FROM deployment_recovery_ledger "
                "WHERE host_key=? AND state IN ('failed', 'uncertain', 'suppressed') LIMIT 1",
                (host_key,),
            ).fetchone()
            if stale is not None:
                self._latch(db, str(stale["source_receipt_id"]), "terminal", timestamp)
            result = []
            for row in rows:
                current = db.execute(
                    "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                    (row["source_receipt_id"],),
                ).fetchone()
                result.append(self._record(current))
            return tuple(result)

    def active_records(self, host_key: str, *, limit: int) -> tuple[DeploymentRecoveryRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32:
            raise ValueError("limit must be between 1 and 32")
        with self._reader() as db:
            rows = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE host_key=? "
                "AND state IN ('staged', 'awaiting_verdict') "
                "ORDER BY created_at, source_receipt_id LIMIT ?",
                (host_key, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def mark_suppressed(
        self, source_receipt_id: str, *, reason: str = "staging_failed", now: float | None = None,
    ) -> DeploymentRecoveryRecord:
        if reason not in {"staging_failed", "preflight_changed"}:
            raise ValueError("suppression reason is not allowlisted")
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone()
            if row is None or row["state"] not in {PREPARED, STAGED}:
                raise ValueError("recovery intent cannot be suppressed")
            db.execute(
                "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                "WHERE source_receipt_id=?",
                (SUPPRESSED, timestamp, timestamp, source_receipt_id),
            )
            self._latch(db, source_receipt_id, reason, timestamp)
            return self._record(db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (source_receipt_id,),
            ).fetchone())

    def observe_receipt(
        self, record: DeploymentRecoveryRecord, deploy: DeployRecord | None, *, now: float | None = None,
    ) -> DeploymentRecoveryRecord | None:
        """Advance a pre-existing receipt or fence it at its fixed deadline."""
        timestamp = self._now(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (record.source_receipt_id,),
            ).fetchone()
            if row is None or row["state"] not in {STAGED, AWAITING_VERDICT}:
                return None
            if deploy is not None and deploy.status == DEGRADED:
                db.execute(
                    "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                    "WHERE source_receipt_id=?",
                    (FAILED, timestamp, timestamp, record.source_receipt_id),
                )
                self._latch(db, record.source_receipt_id, "failed_verification", timestamp)
            elif deploy is not None and deploy.status == HEALTHY:
                db.execute(
                    "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                    "WHERE source_receipt_id=?",
                    (RESOLVED, timestamp, timestamp, record.source_receipt_id),
                )
            elif float(row["deadline_at"]) <= timestamp:
                db.execute(
                    "UPDATE deployment_recovery_ledger SET state=?, updated_at=?, completed_at=? "
                    "WHERE source_receipt_id=?",
                    (UNCERTAIN, timestamp, timestamp, record.source_receipt_id),
                )
                self._latch(db, record.source_receipt_id, "deadline", timestamp)
            elif (row["state"] == STAGED and deploy is not None
                    and deploy.restart_confirmed_at is not None):
                db.execute(
                    "UPDATE deployment_recovery_ledger SET state=?, updated_at=? "
                    "WHERE source_receipt_id=?",
                    (AWAITING_VERDICT, timestamp, record.source_receipt_id),
                )
            current = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE source_receipt_id=?",
                (record.source_receipt_id,),
            ).fetchone()
            result = self._record(current)
            return result if result.state in _HALTING_STATES | {RESOLVED} else None

    def next_incident(self, host_key: str) -> DeploymentRecoveryRecord | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM deployment_recovery_ledger WHERE host_key=? "
                "AND state IN ('failed', 'uncertain', 'suppressed') "
                "AND incident_recorded_at IS NULL ORDER BY completed_at, source_receipt_id LIMIT 1",
                (host_key,),
            ).fetchone()
            return None if row is None else self._record(row)

    def mark_incident_recorded(self, source_receipt_id: str, *, now: float | None = None) -> None:
        timestamp = self._now(now)
        with self._transaction() as db:
            db.execute(
                "UPDATE deployment_recovery_ledger SET incident_recorded_at=? "
                "WHERE source_receipt_id=? AND state IN ('failed', 'uncertain', 'suppressed') "
                "AND incident_recorded_at IS NULL",
                (timestamp, source_receipt_id),
            )

    def requires_pause(self, host_key: str) -> bool:
        with self._reader() as db:
            return db.execute(
                "SELECT 1 FROM deployment_recovery_ledger WHERE host_key=? "
                "AND state IN ('prepared', 'staged', 'awaiting_verdict', "
                "'failed', 'uncertain', 'suppressed') LIMIT 1",
                (host_key,),
            ).fetchone() is not None


Restart = Callable[[str], Awaitable[bool]]


class DeploymentRecoveryController:
    """Strict one-shot same-revision recovery; no checkout or release changes."""

    def __init__(
        self,
        ledger: DeployLedger,
        *,
        state_db: str | Path,
        host_key: str,
        repo_root: Path,
        gateway_health: object,
        systemctl_scope: str,
        settling_seconds: float,
        recovery_deadline_seconds: float,
        enabled: bool,
        restart: Restart,
    ) -> None:
        if systemctl_scope not in {"system", "user"}:
            raise ValueError("systemctl scope must be system or user")
        if not callable(restart):
            raise ValueError("restart must be callable")
        if not isinstance(enabled, bool):
            raise ValueError("recovery enabled must be a boolean")
        if (isinstance(recovery_deadline_seconds, bool)
                or not isinstance(recovery_deadline_seconds, (int, float))
                or not math.isfinite(recovery_deadline_seconds)
                or not 1 <= recovery_deadline_seconds <= _MAX_RECOVERY_DEADLINE_SECONDS):
            raise ValueError("recovery deadline must be finite and bounded")
        self._deploy = ledger
        self._records = DeploymentRecoveryLedger(state_db)
        self._safety = SafetyStateStore(state_db)
        self._host_key = host_key
        self._root = repo_root.resolve(strict=True)
        self._health = gateway_health
        self._scope = systemctl_scope
        self._settling = settling_seconds
        self._deadline = float(recovery_deadline_seconds)
        self._enabled = enabled
        self._restart = restart
        self._records.reconcile_startup(host_key)

    @property
    def autonomy_halted(self) -> bool:
        return self._safety.is_latched(_HALT_NAME) or self._records.requires_pause(self._host_key)

    def reconcile_verdicts(self) -> tuple[DeploymentRecoveryRecord, ...]:
        """Consume only a final verifier verdict for an already-issued restart."""
        outcomes: list[DeploymentRecoveryRecord] = []
        for record in self._records.active_records(self._host_key, limit=8):
            deploy = self._deploy.get(record.recovery_receipt_id)
            finished = self._records.observe_receipt(record, deploy)
            if finished is not None:
                outcomes.append(finished)
        return tuple(outcomes)

    def next_incident(self) -> DeploymentRecoveryRecord | None:
        return self._records.next_incident(self._host_key)

    def mark_incident_recorded(self, source_receipt_id: str) -> None:
        self._records.mark_incident_recorded(source_receipt_id)

    async def recover_one(self) -> DeploymentRecoveryRecord | None:
        """Attempt at most one strictly eligible recovery; otherwise do nothing."""
        if not self._enabled or self.autonomy_halted:
            return None
        for source in self._deploy.degraded_records(self._host_key, limit=8):
            if self._records.get(source.id) is not None:
                continue
            process_id = await self._eligible(source)
            if process_id is None:
                continue
            intent = self._records.begin(source)
            if intent is None:
                continue
            try:
                staged = self._deploy.schedule(
                    source.run_id, "gateway", "systemctl", self._host_key,
                    source.expected_sha, baseline_sha=source.baseline_sha,
                    settling_seconds=self._settling, await_restart=True,
                    baseline_process_id=process_id, exclusive=True,
                    systemctl_scope=self._scope,
                )
                self._records.stage(
                    source.id, staged.id,
                    deadline_seconds=self._deadline,
                )
            except (OSError, ValueError):
                return self._records.mark_suppressed(source.id)
            rechecked_process_id = await self._eligible(source)
            if rechecked_process_id != process_id:
                return self._records.mark_suppressed(source.id, reason="preflight_changed")
            try:
                restarted = await self._restart(self._scope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - no raw command failure enters durable state
                restarted = False
            outcome = self._records.mark_command(source.id, success=restarted)
            return outcome
        return None

    async def _eligible(self, source: DeployRecord) -> str | None:
        if (
            source.status != DEGRADED or source.target != "gateway" or source.mode != "systemctl"
            or source.systemctl_scope != self._scope or not source.baseline_sha
            or source.baseline_sha != source.expected_sha or not source.started_process_id
        ):
            return None
        revision = await asyncio.to_thread(detect_source_revision, self._root)
        if revision is None or revision.lower() != source.expected_sha:
            return None
        current_identity = await self._health.current_process_identity()
        if current_identity is None:
            return None
        process_id, pid = current_identity
        if (not isinstance(process_id, str) or process_id != source.started_process_id
                or not isinstance(pid, int) or pid <= 0):
            return None
        observed = await self._health.revision(source)
        if not isinstance(observed, str) or observed.lower() != source.expected_sha:
            return None
        managed = await asyncio.to_thread(
            is_managed_gateway_process, pid=pid, scope=self._scope,
        )
        return process_id if managed else None
