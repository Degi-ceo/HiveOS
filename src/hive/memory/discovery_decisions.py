"""Durable, append-only provenance records for discovery and adoption decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

_OUTCOMES = {"found", "no_match", "reused", "adopted", "rejected", "deferred", "requires_review"}
_AUDITS = {"passed", "failed", "unavailable", "not_run"}


@dataclass(frozen=True, slots=True)
class DiscoveryDecision:
    decision_id: str
    idempotency_key: str
    capability_key: str
    phase: str
    outcome: str
    candidate_name: str
    candidate_source: str
    candidate_url: str
    pinned_version: str
    source_revision: str
    license_spdx: str
    audit_status: str
    evidence_digest: str
    rationale: str
    recorded_by: str
    created_ts: float
    supersedes_decision_id: str | None


class DiscoveryDecisionStore:
    """Append-only audit records; this store never installs, enables, or invokes a candidate."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._clock = clock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS discovery_decisions(
          decision_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
          capability_key TEXT NOT NULL, phase TEXT NOT NULL, outcome TEXT NOT NULL,
          candidate_name TEXT NOT NULL, candidate_source TEXT NOT NULL, candidate_url TEXT NOT NULL,
          pinned_version TEXT NOT NULL, source_revision TEXT NOT NULL, license_spdx TEXT NOT NULL,
          audit_status TEXT NOT NULL, evidence_digest TEXT NOT NULL, rationale TEXT NOT NULL,
          recorded_by TEXT NOT NULL, created_ts REAL NOT NULL, supersedes_decision_id TEXT)""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS discovery_decisions_capability "
            "ON discovery_decisions(capability_key, created_ts DESC)"
        )
        self._db.commit()

    @staticmethod
    def _bounded(value: object, *, limit: int = 500) -> str:
        return str(value or "").strip()[:limit]

    @classmethod
    def _safe_url(cls, value: object) -> str:
        raw = cls._bounded(value)
        if "://" not in raw:
            return raw
        parsed = urlsplit(raw)
        if parsed.username is not None or parsed.password is not None:
            return ""
        return cls._bounded(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))

    def record(
        self, *, capability_key: str, phase: str, outcome: str, idempotency_key: str,
        candidate_name: str = "", candidate_source: str = "", candidate_url: str = "",
        pinned_version: str = "", source_revision: str = "", license_spdx: str = "",
        audit_status: str = "not_run", rationale: str = "", recorded_by: str = "hive",
        supersedes_decision_id: str | None = None,
    ) -> DiscoveryDecision:
        if phase not in {"discovery", "adoption"} or outcome not in _OUTCOMES or audit_status not in _AUDITS:
            raise ValueError("invalid decision enum")
        capability_key = self._bounded(capability_key, limit=200)
        idempotency_key = self._bounded(idempotency_key, limit=200)
        if not capability_key or not idempotency_key:
            raise ValueError("capability_key and idempotency_key are required")
        if outcome == "adopted" and (audit_status != "passed" or not (pinned_version or source_revision)):
            raise ValueError("adopted requires passed audit and immutable version or revision")
        values = [
            self._bounded(value)
            for value in (
                candidate_name, candidate_source, self._safe_url(candidate_url), pinned_version,
                source_revision, license_spdx, rationale, recorded_by,
            )
        ]
        evidence = hashlib.sha256(
            json.dumps([capability_key, phase, outcome, *values, audit_status], sort_keys=True).encode()
        ).hexdigest()
        with self._db:
            row = self._db.execute(
                "SELECT * FROM discovery_decisions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row:
                return DiscoveryDecision(**dict(row))
            decision_id = f"disc_{uuid.uuid4().hex}"
            self._db.execute(
                "INSERT INTO discovery_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, idempotency_key, capability_key, phase, outcome, *values[:6],
                    audit_status, evidence, values[6], values[7], self._clock(), supersedes_decision_id,
                ),
            )
        return self.get(decision_id)

    def get(self, decision_id: str) -> DiscoveryDecision:
        row = self._db.execute(
            "SELECT * FROM discovery_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return DiscoveryDecision(**dict(row))

    def latest(self, capability_key: str) -> DiscoveryDecision | None:
        row = self._db.execute(
            "SELECT * FROM discovery_decisions WHERE capability_key=? ORDER BY created_ts DESC, decision_id DESC LIMIT 1",
            (self._bounded(capability_key, limit=200),),
        ).fetchone()
        return DiscoveryDecision(**dict(row)) if row else None

    def list_for_capability(self, capability_key: str, *, limit: int = 20) -> list[DiscoveryDecision]:
        rows = self._db.execute(
            "SELECT * FROM discovery_decisions WHERE capability_key=? "
            "ORDER BY created_ts DESC, decision_id DESC LIMIT ?",
            (self._bounded(capability_key, limit=200), max(1, min(int(limit), 100))),
        ).fetchall()
        return [DiscoveryDecision(**dict(row)) for row in rows]

    def close(self) -> None:
        self._db.close()
