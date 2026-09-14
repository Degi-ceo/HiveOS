"""Durable, redacted incident lifecycle for bounded Hive recovery.

This is deliberately an operator record, not a transcript or diagnostic dump.
Only a redacted summary and structured identifiers are persisted.  Recovery is
state-machine based so callers cannot turn an incident into arbitrary command
execution.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from hive.core.redact import redact_value

OPEN_STATUSES = frozenset({"open", "diagnosing", "recovering", "awaiting_review"})
STATUSES = frozenset({*OPEN_STATUSES, "resolved", "suppressed"})
SEVERITIES = frozenset({"warning", "error", "critical"})


class IncidentLedger:
    """SQLite-backed incidents with atomic active-fingerprint de-duplication."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        self._clock = clock
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._db:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS hive_incidents(
                incident_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                task_id INTEGER,
                recovery_count INTEGER NOT NULL DEFAULT 0,
                next_recovery_ts REAL NOT NULL DEFAULT 0,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                resolved_ts REAL
              );
              CREATE UNIQUE INDEX IF NOT EXISTS hive_incidents_active_fingerprint
                ON hive_incidents(fingerprint)
                WHERE status IN ('open', 'diagnosing', 'recovering', 'awaiting_review');
              CREATE INDEX IF NOT EXISTS hive_incidents_recent
                ON hive_incidents(updated_ts DESC);
              CREATE TABLE IF NOT EXISTS hive_incident_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(incident_id) REFERENCES hive_incidents(incident_id)
              );
              CREATE INDEX IF NOT EXISTS hive_incident_events_incident
                ON hive_incident_events(incident_id, id);
            """)
            # Existing M6 databases have the narrower partial index. Rebuild it
            # so a remediation awaiting human review remains de-duplicated.
            self._db.execute("DROP INDEX IF EXISTS hive_incidents_active_fingerprint")
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS hive_incidents_active_fingerprint "
                "ON hive_incidents(fingerprint) "
                "WHERE status IN ('open', 'diagnosing', 'recovering', 'awaiting_review')"
            )

    @staticmethod
    def _safe_summary(value: object) -> str:
        return str(redact_value(value)).replace("\n", " ").strip()[:500] or "unspecified failure"

    def record(self, source: str, summary: object, *, severity: str = "error",
               run_id: str = "", task_id: int | None = None,
               evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create or update one active incident without storing raw failure data."""
        normalized_source = str(source).strip().lower()[:80] or "unknown"
        normalized_severity = str(severity).lower()
        if normalized_severity not in SEVERITIES:
            normalized_severity = "error"
        safe_summary = self._safe_summary(summary)
        fingerprint = hashlib.sha256(
            f"{normalized_source}\0{safe_summary.casefold()}".encode("utf-8")
        ).hexdigest()
        now = self._clock()
        safe_evidence = redact_value(dict(evidence or {}))
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT incident_id FROM hive_incidents WHERE fingerprint=? "
                "AND status IN ('open', 'diagnosing', 'recovering', 'awaiting_review')", (fingerprint,),
            ).fetchone()
            if row is None:
                incident_id = str(uuid.uuid4())
                try:
                    self._db.execute(
                        "INSERT INTO hive_incidents(incident_id, fingerprint, source, severity, status, summary, "
                        "run_id, task_id, created_ts, updated_ts) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                        (incident_id, fingerprint, normalized_source, normalized_severity, safe_summary,
                         str(run_id or ""), task_id, now, now),
                    )
                    event_type = "detected"
                except sqlite3.IntegrityError:
                    # Another process won the partial-unique-index race. Reuse
                    # its active incident rather than creating duplicate recovery.
                    row = self._db.execute(
                        "SELECT incident_id FROM hive_incidents WHERE fingerprint=? "
                        "AND status IN ('open', 'diagnosing', 'recovering', 'awaiting_review')", (fingerprint,),
                    ).fetchone()
                    if row is None:
                        raise
                    incident_id = str(row["incident_id"])
                    event_type = "repeated"
            else:
                incident_id = str(row["incident_id"])
                self._db.execute(
                    "UPDATE hive_incidents SET updated_ts=?, run_id=CASE WHEN run_id='' THEN ? ELSE run_id END, "
                    "task_id=COALESCE(task_id, ?) WHERE incident_id=?",
                    (now, str(run_id or ""), task_id, incident_id),
                )
                event_type = "repeated"
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type, evidence_json) VALUES (?, ?, ?, ?)",
                (incident_id, now, event_type, json.dumps(safe_evidence, sort_keys=True, default=str)),
            )
        return self.get(incident_id) or {"incident_id": incident_id}

    def get(self, incident_id: str, *, include_events: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM hive_incidents WHERE incident_id=?", (str(incident_id),)).fetchone()
            if row is None:
                return None
            result = dict(row)
            if include_events:
                events = self._db.execute(
                    "SELECT id, ts, event_type, evidence_json FROM hive_incident_events "
                    "WHERE incident_id=? ORDER BY id DESC LIMIT 200", (str(incident_id),),
                ).fetchall()
                # Operators need the most recent bounded evidence (for example a
                # recovery result or PR reference), but presentation remains
                # chronological within that bounded window.
                events = list(reversed(events))
                result["events"] = [
                    {"id": int(item["id"]), "ts": float(item["ts"]), "type": str(item["event_type"]),
                     "evidence": json.loads(str(item["evidence_json"]))}
                    for item in events
                ]
        return result

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                "SELECT incident_id, source, severity, status, summary, run_id, task_id, recovery_count, "
                "next_recovery_ts, created_ts, updated_ts, resolved_ts FROM hive_incidents "
                "ORDER BY updated_ts DESC LIMIT ?", (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, incident_id: str) -> bool:
        return self._transition(incident_id, from_statuses=OPEN_STATUSES, to_status="suppressed", event_type="acknowledged")

    def begin_diagnosis(self, incident_id: str, *, run_id: str) -> dict[str, Any] | None:
        """Claim one open incident for a correlated, reviewable diagnosis run."""
        now = self._clock()
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_incidents SET status='diagnosing', run_id=CASE WHEN run_id='' THEN ? ELSE run_id END, "
                "updated_ts=? WHERE incident_id=? AND status='open'",
                (str(run_id), now, str(incident_id)),
            )
            if cursor.rowcount != 1:
                return None
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type, evidence_json) VALUES (?, ?, ?, ?)",
                (str(incident_id), now, "diagnosis_started", json.dumps({"run_id": str(run_id)})),
            )
        return self.get(incident_id)

    def record_remediation(self, incident_id: str, *, status: str,
                           evidence: dict[str, Any] | None = None) -> bool:
        """Persist safe diagnosis/branch/PR/review metadata for an active incident."""
        if status not in {"open", "awaiting_review", "resolved"}:
            raise ValueError("unsupported remediation status")
        now = self._clock()
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_incidents SET status=?, updated_ts=?, resolved_ts=? "
                "WHERE incident_id=? AND status='diagnosing'",
                (status, now, now if status == "resolved" else None, str(incident_id)),
            )
            if cursor.rowcount != 1:
                return False
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type, evidence_json) VALUES (?, ?, ?, ?)",
                (str(incident_id), now, "remediation_recorded",
                 json.dumps(redact_value(dict(evidence or {})), sort_keys=True, default=str)),
            )
        return True

    def links(self, incident_id: str) -> dict[str, Any] | None:
        """Return only stable run/branch/PR/review references from safe events."""
        incident = self.get(incident_id)
        if incident is None:
            return None
        refs: list[dict[str, Any]] = []
        for event in incident.get("events", []):
            evidence = event.get("evidence") if isinstance(event, dict) else None
            if not isinstance(evidence, dict):
                continue
            shared = {key: evidence[key] for key in (
                "run_id", "review_state", "ci_status", "checks_failed", "checks_pending",
            ) if key in evidence}
            # Runtime diagnosis persists a list because one diagnosis may create
            # more than one candidate branch/PR.  Expand only its stable IDs;
            # never project model output, arguments, or free-form evidence.
            remediation_refs = evidence.get("remediation_refs")
            if isinstance(remediation_refs, list):
                for candidate in remediation_refs[:20]:
                    if not isinstance(candidate, dict):
                        continue
                    safe = {key: candidate[key] for key in ("branch", "pr_url")
                            if isinstance(candidate.get(key), str) and candidate[key]}
                    if safe:
                        refs.append({"event": event.get("type"), **shared, **safe})
                if shared and not any(isinstance(candidate, dict) for candidate in remediation_refs[:20]):
                    refs.append({"event": event.get("type"), **shared})
                continue
            # Preserve compatibility with already-persisted singular references.
            safe = {key: evidence[key] for key in ("branch", "pr_url")
                    if isinstance(evidence.get(key), str) and evidence[key]}
            if shared or safe:
                refs.append({"event": event.get("type"), **shared, **safe})
        return {"incident_id": incident["incident_id"], "status": incident["status"], "links": refs}

    def begin_recovery(self, incident_id: str, *, cooldown_seconds: float = 60.0,
                       max_recoveries: int = 3) -> dict[str, Any] | None:
        now = self._clock()
        recovery_limit = max(1, int(max_recoveries))
        with self._lock, self._db:
            # Keep eligibility in the mutation predicate: an in-process lock
            # cannot serialize independent Hive processes sharing this database.
            cursor = self._db.execute(
                "UPDATE hive_incidents SET status='recovering', recovery_count=recovery_count+1, "
                "next_recovery_ts=?, updated_ts=? WHERE incident_id=? "
                "AND status IN ('open', 'diagnosing') "
                "AND recovery_count < ? AND next_recovery_ts <= ?",
                (now + max(0.0, cooldown_seconds), now, str(incident_id), recovery_limit, now),
            )
            if cursor.rowcount != 1:
                return None
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type) VALUES (?, ?, 'recovery_started')",
                (str(incident_id), now),
            )
        return self.get(incident_id)

    def finish_recovery(self, incident_id: str, *, resolved: bool, evidence: dict[str, Any] | None = None) -> bool:
        now = self._clock()
        status = "resolved" if resolved else "open"
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_incidents SET status=?, updated_ts=?, resolved_ts=? "
                "WHERE incident_id=? AND status='recovering'",
                (status, now, now if resolved else None, str(incident_id)),
            )
            if cursor.rowcount != 1:
                return False
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type, evidence_json) VALUES (?, ?, ?, ?)",
                (str(incident_id), now, "recovery_resolved" if resolved else "recovery_failed",
                 json.dumps(redact_value(dict(evidence or {})), sort_keys=True, default=str)),
            )
        return True

    def _transition(self, incident_id: str, *, from_statuses: frozenset[str], to_status: str, event_type: str) -> bool:
        now = self._clock()
        clauses = ", ".join("?" for _ in from_statuses)
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE hive_incidents SET status=?, updated_ts=?, resolved_ts=? WHERE incident_id=? AND status IN ({clauses})",
                (to_status, now, now if to_status in {"resolved", "suppressed"} else None,
                 str(incident_id), *sorted(from_statuses)),
            )
            if cursor.rowcount != 1:
                return False
            self._db.execute(
                "INSERT INTO hive_incident_events(incident_id, ts, event_type) VALUES (?, ?, ?)",
                (str(incident_id), now, event_type),
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()
