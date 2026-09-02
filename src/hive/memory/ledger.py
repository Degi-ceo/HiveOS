"""Canonical versioned memory ledger; projections are derived, never authoritative."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class MemoryVersion:
    memory_id: str
    version: int
    kind: str
    stable_key: str
    content: str
    source: str
    status: str
    content_hash: str
    provenance_kind: str = "unknown"
    confidence: float = 0.5
    observed_ts: float | None = None
    fresh_until_ts: float | None = None
    veracity: str = "unknown"
    correction_of_version: int | None = None
    correction_reason: str | None = None


class MemoryLedger:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_items(memory_id TEXT PRIMARY KEY, stable_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, current_version INTEGER NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_versions(memory_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL, created_ts REAL NOT NULL, provenance_kind TEXT NOT NULL DEFAULT 'unknown', confidence REAL NOT NULL DEFAULT 0.5, observed_ts REAL, fresh_until_ts REAL, veracity TEXT NOT NULL DEFAULT 'unknown', correction_of_version INTEGER, correction_reason TEXT, PRIMARY KEY(memory_id,version));
        CREATE TABLE IF NOT EXISTS memory_events(event_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_projection_outbox(operation_id TEXT PRIMARY KEY, target TEXT NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, operation TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL, replay_safe INTEGER NOT NULL DEFAULT 0, worker_id TEXT, lease_until REAL, last_error TEXT, UNIQUE(target,memory_id,version,operation));
        CREATE TABLE IF NOT EXISTS memory_projection_bindings(target TEXT NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, external_id TEXT NOT NULL, PRIMARY KEY(target,memory_id,version));
        """)
        self._migrate_memory_versions()
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(memory_projection_outbox)")}
        if "replay_safe" not in columns:
            self._db.execute("ALTER TABLE memory_projection_outbox ADD COLUMN replay_safe INTEGER NOT NULL DEFAULT 0")
        if "worker_id" not in columns:
            self._db.execute("ALTER TABLE memory_projection_outbox ADD COLUMN worker_id TEXT")
        if "lease_until" not in columns:
            self._db.execute("ALTER TABLE memory_projection_outbox ADD COLUMN lease_until REAL")
        if "last_error" not in columns:
            self._db.execute("ALTER TABLE memory_projection_outbox ADD COLUMN last_error TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS memory_projection_outbox_claim ON memory_projection_outbox(target, state, lease_until)")
        self._db.commit()

    def _migrate_memory_versions(self) -> None:
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(memory_versions)")}
        additions = {
            "provenance_kind": "TEXT NOT NULL DEFAULT 'unknown'",
            "confidence": "REAL NOT NULL DEFAULT 0.5",
            "observed_ts": "REAL",
            "fresh_until_ts": "REAL",
            "veracity": "TEXT NOT NULL DEFAULT 'unknown'",
            "correction_of_version": "INTEGER",
            "correction_reason": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE memory_versions ADD COLUMN {name} {definition}")
        self._db.execute("UPDATE memory_versions SET observed_ts=created_ts WHERE observed_ts IS NULL")

    @staticmethod
    def _validate_claim_metadata(
        *, provenance_kind: str, confidence: float, observed_ts: float | None,
        fresh_until_ts: float | None, veracity: str,
    ) -> None:
        if provenance_kind not in {"human", "agent", "tool", "imported", "system", "unknown"}:
            raise ValueError("invalid provenance_kind")
        if veracity not in {"stated", "inferred", "tool", "imported", "unknown"}:
            raise ValueError("invalid veracity")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        for name, timestamp in (("observed_ts", observed_ts), ("fresh_until_ts", fresh_until_ts)):
            if timestamp is not None and not math.isfinite(timestamp):
                raise ValueError(f"{name} must be finite")
        if observed_ts is not None and fresh_until_ts is not None and fresh_until_ts < observed_ts:
            raise ValueError("fresh_until_ts cannot precede observed_ts")

    def _append_version(
        self, *, memory_id: str, version: int, content: str, source: str,
        created_ts: float, provenance_kind: str, confidence: float,
        observed_ts: float | None, fresh_until_ts: float | None, veracity: str,
        correction_of_version: int | None = None, correction_reason: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO memory_versions(memory_id,version,content,source,content_hash,created_ts,"
            "provenance_kind,confidence,observed_ts,fresh_until_ts,veracity,correction_of_version,correction_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, version, content, source, hashlib.sha256(content.encode()).hexdigest(),
                created_ts, provenance_kind, confidence, observed_ts if observed_ts is not None else created_ts,
                fresh_until_ts, veracity, correction_of_version, correction_reason,
            ),
        )

    def remember(
        self, *, kind: str, stable_key: str, content: str, source: str, idempotency_key: str,
        targets: tuple[str, ...] = ("mnemosyne", "obsidian"), provenance_kind: str = "unknown",
        confidence: float = 0.5, observed_ts: float | None = None,
        fresh_until_ts: float | None = None, veracity: str = "unknown",
    ) -> MemoryVersion:
        if not stable_key.strip() or not content.strip() or not idempotency_key.strip():
            raise ValueError("stable_key, content and idempotency_key are required")
        now = self._clock()
        effective_observed_ts = observed_ts if observed_ts is not None else now
        self._validate_claim_metadata(
            provenance_kind=provenance_kind, confidence=confidence, observed_ts=effective_observed_ts,
            fresh_until_ts=fresh_until_ts, veracity=veracity,
        )
        with self._db:
            duplicate = self._db.execute(
                "SELECT memory_id,version FROM memory_events WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self.get_version(duplicate["memory_id"], int(duplicate["version"]))
            prior = self._db.execute("SELECT memory_id,current_version FROM memory_items WHERE stable_key=?", (stable_key,)).fetchone()
            memory_id = prior["memory_id"] if prior else f"mem_{uuid.uuid4().hex}"
            version = int(prior["current_version"]) + 1 if prior else 1
            if prior:
                self._db.execute("UPDATE memory_items SET kind=?,status='active',current_version=?,updated_ts=? WHERE memory_id=?", (kind, version, now, memory_id))
            else:
                self._db.execute("INSERT INTO memory_items VALUES(?,?,?,?,?,?,?)", (memory_id, stable_key, kind, "active", version, now, now))
            self._append_version(memory_id=memory_id, version=version, content=content, source=source, created_ts=now, provenance_kind=provenance_kind, confidence=confidence, observed_ts=effective_observed_ts, fresh_until_ts=fresh_until_ts, veracity=veracity)
            self._db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", idempotency_key, memory_id, version, "created" if version == 1 else "superseded", json.dumps({"source": source}), now))
            for target in targets:
                self._db.execute("INSERT INTO memory_projection_outbox(operation_id,target,memory_id,version,operation,created_ts,replay_safe) VALUES(?,?,?,?,?,?,?)", (f"proj_{uuid.uuid4().hex}", target, memory_id, version, "upsert", now, int(target == "obsidian")))
        return self.get_current(memory_id)

    def correct(
        self, *, content: str, source: str, actor: str, reason: str, idempotency_key: str,
        memory_id: str | None = None, stable_key: str | None = None,
        targets: tuple[str, ...] = ("mnemosyne", "obsidian"), confidence: float = 1.0,
        observed_ts: float | None = None, fresh_until_ts: float | None = None,
        veracity: str = "stated",
    ) -> MemoryVersion:
        if not content.strip() or not actor.strip() or not reason.strip() or not idempotency_key.strip():
            raise ValueError("content, actor, reason and idempotency_key are required")
        if bool(memory_id and memory_id.strip()) == bool(stable_key and stable_key.strip()):
            raise ValueError("exactly one of memory_id or stable_key is required")
        now = self._clock()
        effective_observed_ts = observed_ts if observed_ts is not None else now
        self._validate_claim_metadata(provenance_kind="human", confidence=confidence, observed_ts=effective_observed_ts, fresh_until_ts=fresh_until_ts, veracity=veracity)
        with self._db:
            duplicate = self._db.execute(
                "SELECT memory_id,version FROM memory_events WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self.get_version(duplicate["memory_id"], int(duplicate["version"]))
            if memory_id and memory_id.strip():
                prior = self._db.execute("SELECT memory_id,kind,stable_key,current_version FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
                identity = memory_id
            else:
                prior = self._db.execute("SELECT memory_id,kind,stable_key,current_version FROM memory_items WHERE stable_key=?", (stable_key,)).fetchone()
                identity = stable_key
            if prior is None:
                raise KeyError(identity)
            resolved_memory_id = str(prior["memory_id"])
            version = int(prior["current_version"]) + 1
            self._db.execute("UPDATE memory_items SET current_version=?,updated_ts=? WHERE memory_id=?", (version, now, resolved_memory_id))
            self._append_version(memory_id=resolved_memory_id, version=version, content=content, source=source, created_ts=now, provenance_kind="human", confidence=confidence, observed_ts=effective_observed_ts, fresh_until_ts=fresh_until_ts, veracity=veracity, correction_of_version=version - 1, correction_reason=reason)
            self._db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", idempotency_key, resolved_memory_id, version, "corrected", json.dumps({"actor": actor, "source": source}), now))
            for target in targets:
                self._db.execute("INSERT INTO memory_projection_outbox(operation_id,target,memory_id,version,operation,created_ts,replay_safe) VALUES(?,?,?,?,?,?,?)", (f"proj_{uuid.uuid4().hex}", target, resolved_memory_id, version, "upsert", now, int(target == "obsidian")))
        return self.get_current(resolved_memory_id)

    def get_current(self, memory_id: str) -> MemoryVersion:
        row = self._db.execute("SELECT i.memory_id,i.current_version AS version,i.kind,i.stable_key,v.content,v.source,i.status,v.content_hash,v.provenance_kind,v.confidence,v.observed_ts,v.fresh_until_ts,v.veracity,v.correction_of_version,v.correction_reason FROM memory_items i JOIN memory_versions v ON v.memory_id=i.memory_id AND v.version=i.current_version WHERE i.memory_id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return MemoryVersion(**dict(row))

    def get_version(self, memory_id: str, version: int) -> MemoryVersion:
        row = self._db.execute("SELECT i.memory_id,v.version,i.kind,i.stable_key,v.content,v.source,i.status,v.content_hash,v.provenance_kind,v.confidence,v.observed_ts,v.fresh_until_ts,v.veracity,v.correction_of_version,v.correction_reason FROM memory_items i JOIN memory_versions v ON v.memory_id=i.memory_id WHERE i.memory_id=? AND v.version=?", (memory_id, version)).fetchone()
        if row is None:
            raise KeyError(f"{memory_id}@{version}")
        return MemoryVersion(**dict(row))
    def prompt_claims(self, *, limit: int = 5) -> list[dict]:
        """Select only trusted durable claims safe for static model context.

        Session transcripts and ordinary agent memory are deliberately excluded:
        they may contain another user's data or untrusted instructions. An empty
        result remains authoritative and never enables a legacy fallback.
        """
        if limit < 1:
            return []
        now = self._clock()
        kinds = ("fact", "skill", "mcp", "research", "fix")
        provenances = ("human", "system", "tool")
        placeholders = ",".join("?" for _ in kinds)
        provenance_placeholders = ",".join("?" for _ in provenances)
        rows = self._db.execute(
            "SELECT i.memory_id,i.current_version AS version,i.kind,i.stable_key,v.content,v.source,"
            "i.status,v.content_hash,v.provenance_kind,v.confidence,v.observed_ts,v.fresh_until_ts,"
            "v.veracity,v.correction_of_version,v.correction_reason "
            "FROM memory_items i JOIN memory_versions v ON v.memory_id=i.memory_id "
            "AND v.version=i.current_version "
            f"WHERE i.kind IN ({placeholders}) AND v.provenance_kind IN ({provenance_placeholders}) "
            "AND (v.fresh_until_ts IS NULL OR v.fresh_until_ts >= ?) "
            "ORDER BY (v.correction_of_version IS NOT NULL) DESC, "
            "v.confidence DESC, v.observed_ts DESC, i.updated_ts DESC LIMIT ?",
            [*kinds, *provenances, now, limit],
        ).fetchall()
        return self._recall_rows(rows, now)
    def _recall_rows(self, rows: list[sqlite3.Row], now: float) -> list[dict]:
        results: list[dict] = []
        for row in rows:
            memory = MemoryVersion(**dict(row))
            freshness = "expired" if memory.fresh_until_ts is not None and memory.fresh_until_ts < now else "current"
            results.append({
                "memory_id": memory.memory_id,
                "version": memory.version,
                "kind": memory.kind,
                "topic": memory.stable_key,
                "content": memory.content,
                "source": memory.source,
                "explanation": {
                    "provenance_kind": memory.provenance_kind,
                    "confidence": memory.confidence,
                    "freshness": freshness,
                    "correction_of_version": memory.correction_of_version,
                },
            })
        return results
    def recall_current(self, query: str, *, limit: int = 5,
                       include_expired: bool = False) -> list[dict]:
        """Select current canonical claims with non-secret selection evidence.

        This deliberately uses SQLite substring matching rather than an auxiliary
        index: the ledger is the consistency boundary, and callers can safely
        fall back to a narrower result without reviving a superseded local row.
        """
        if not query.strip() or limit < 1:
            return []
        now = self._clock()
        pattern = f"%{query}%"
        expiry_clause = "" if include_expired else "AND (v.fresh_until_ts IS NULL OR v.fresh_until_ts >= ?)"
        params: list[object] = [pattern, pattern]
        if not include_expired:
            params.append(now)
        params.append(limit)
        rows = self._db.execute(
            "SELECT i.memory_id,i.current_version AS version,i.kind,i.stable_key,v.content,v.source,"
            "i.status,v.content_hash,v.provenance_kind,v.confidence,v.observed_ts,v.fresh_until_ts,"
            "v.veracity,v.correction_of_version,v.correction_reason "
            "FROM memory_items i JOIN memory_versions v ON v.memory_id=i.memory_id "
            "AND v.version=i.current_version WHERE (i.stable_key LIKE ? OR v.content LIKE ?) "
            f"{expiry_clause} ORDER BY (v.correction_of_version IS NOT NULL) DESC, "
            "v.confidence DESC, v.observed_ts DESC, i.updated_ts DESC LIMIT ?",
            params,
        ).fetchall()
        return self._recall_rows(rows, now)
    def pending_projections(self, target: str) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM memory_projection_outbox WHERE target=? AND state='pending' ORDER BY created_ts", (target,))]

    def projection_summary(self) -> dict:
        """Return aggregate-only outbox health for a safe operator surface.

        This deliberately exposes neither claim content nor delivery diagnostics:
        provider exceptions and identifiers can contain sensitive data.  Rows with
        an unexpected state are retained under ``unknown`` rather than hidden.
        """
        known_states = ("pending", "running", "applied", "requires_review")
        targets: dict[str, dict[str, int]] = {}
        for row in self._db.execute(
            "SELECT target,state,COUNT(*) AS count FROM memory_projection_outbox GROUP BY target,state"
        ):
            target = str(row["target"])
            state = str(row["state"])
            bucket = state if state in known_states else "unknown"
            counts = targets.setdefault(target, {name: 0 for name in (*known_states, "unknown", "total")})
            counts[bucket] += int(row["count"])
            counts["total"] += int(row["count"])
        total = sum(counts["total"] for counts in targets.values())
        open_count = sum(
            counts["pending"] + counts["running"] + counts["requires_review"] + counts["unknown"]
            for counts in targets.values()
        )
        return {
            "total": total,
            "open": open_count,
            "requires_review": sum(counts["requires_review"] for counts in targets.values()),
            "targets": dict(sorted(targets.items())),
        }

    def quarantine_expired_external_projections(self) -> int:
        """Fail closed at recovery: never resume an in-flight external effect.

        A process restart invalidates the worker identity even when its wall-clock
        lease has not yet elapsed.  Keeping such a row ``running`` would let a
        stale process issue a remote side effect after another process recovered.
        """
        with self._db:
            cur = self._db.execute(
                "UPDATE memory_projection_outbox SET state='requires_review', worker_id=NULL, lease_until=NULL, "
                "last_error=COALESCE(last_error, 'external delivery interrupted or lease lost; automatic replay is forbidden') "
                "WHERE state='running' AND replay_safe=0",
            )
        return int(cur.rowcount)

    def has_active_projection_claim(self, operation_id: str, *, worker_id: str) -> bool:
        """Return whether this worker still exclusively owns a live projection claim."""
        row = self._db.execute(
            "SELECT 1 FROM memory_projection_outbox "
            "WHERE operation_id=? AND state='running' AND worker_id=? "
            "AND lease_until IS NOT NULL AND lease_until>?",
            (operation_id, worker_id, self._clock()),
        ).fetchone()
        return row is not None

    def claim_pending_projections(self, target: str, *, worker_id: str,
                                  lease_seconds: float = 300.0) -> list[dict]:
        """Claim pending operations and safely reconcile expired leases.

        Obsidian writes are deterministic and replay-safe. A lease that expires
        before its outcome is recorded returns to pending for recovery. A
        Mnemosyne delivery may have crossed an external-effect boundary, so an
        expired lease is quarantined for review rather than invoked again.
        """
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = self._clock()
        lease_until = now + max(1.0, lease_seconds)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE memory_projection_outbox SET state='pending', worker_id=NULL, lease_until=NULL "
                "WHERE target=? AND state='running' AND replay_safe=1 AND lease_until IS NOT NULL AND lease_until<=?",
                (target, now),
            )
            self._db.execute(
                "UPDATE memory_projection_outbox SET state='requires_review', worker_id=NULL, lease_until=NULL, "
                "last_error=COALESCE(last_error, 'lease expired after external delivery; automatic replay is forbidden') "
                "WHERE target=? AND state='running' AND replay_safe=0 AND lease_until IS NOT NULL AND lease_until<=?",
                (target, now),
            )
            rows = self._db.execute(
                "SELECT candidate.operation_id FROM memory_projection_outbox AS candidate "
                "WHERE candidate.target=? AND candidate.state='pending' "
                "AND NOT EXISTS (SELECT 1 FROM memory_projection_outbox AS prior "
                "WHERE prior.target=candidate.target AND prior.memory_id=candidate.memory_id "
                "AND prior.version<candidate.version AND prior.state!='applied') "
                "ORDER BY candidate.created_ts, candidate.memory_id, candidate.version",
                (target,),
            ).fetchall()
            claimed: list[dict] = []
            for row in rows:
                operation_id = str(row["operation_id"])
                cur = self._db.execute(
                    "UPDATE memory_projection_outbox SET state='running', attempts=attempts+1, worker_id=?, lease_until=? "
                    "WHERE operation_id=? AND state='pending'",
                    (worker_id, lease_until, operation_id),
                )
                if cur.rowcount:
                    claimed_row = self._db.execute(
                        "SELECT * FROM memory_projection_outbox WHERE operation_id=?", (operation_id,)
                    ).fetchone()
                    if claimed_row is not None:
                        claimed.append(dict(claimed_row))
            self._db.commit()
            return claimed
        except Exception:
            self._db.rollback()
            raise

    def mark_projected(self, operation_id: str, *, worker_id: str) -> bool:
        with self._db:
            cur = self._db.execute(
                "UPDATE memory_projection_outbox SET state='applied', worker_id=NULL, lease_until=NULL "
                "WHERE operation_id=? AND state='running' AND worker_id=? "
                "AND lease_until IS NOT NULL AND lease_until>?",
                (operation_id, worker_id, self._clock()),
            )
        return cur.rowcount > 0

    def record_projection_failure(self, operation_id: str, *, worker_id: str,
                                  detail: str = "local projection failed") -> bool:
        """Release only a deterministic local projection for a later retry.

        The ``replay_safe`` predicate is persisted with the operation and makes
        this method fail closed for every external target.
        """
        with self._db:
            cur = self._db.execute(
                "UPDATE memory_projection_outbox SET state='pending', worker_id=NULL, lease_until=NULL, last_error=? "
                "WHERE operation_id=? AND state='running' AND worker_id=? AND replay_safe=1",
                (detail, operation_id, worker_id),
            )
            if cur.rowcount == 0:
                cur = self._db.execute(
                    "UPDATE memory_projection_outbox SET state='requires_review', worker_id=NULL, lease_until=NULL, last_error=? "
                    "WHERE operation_id=? AND state='running' AND worker_id=? AND replay_safe=0",
                    (detail, operation_id, worker_id),
                )
        return cur.rowcount > 0

    def quarantine_projection(self, operation_id: str, *, worker_id: str,
                              detail: str) -> bool:
        """Make an uncertain or conflicting projection require human review."""
        with self._db:
            cur = self._db.execute(
                "UPDATE memory_projection_outbox SET state='requires_review', worker_id=NULL, lease_until=NULL, last_error=? "
                "WHERE operation_id=? AND state='running' AND worker_id=?",
                (detail, operation_id, worker_id),
            )
        return cur.rowcount > 0

    def projection_binding(self, target: str, memory_id: str, version: int) -> str | None:
        row = self._db.execute(
            "SELECT external_id FROM memory_projection_bindings "
            "WHERE target=? AND memory_id=? AND version=?",
            (target, memory_id, version),
        ).fetchone()
        return str(row["external_id"]) if row else None

    def record_projection_binding(
        self, target: str, memory_id: str, version: int, external_id: str,
        *, operation_id: str | None = None, worker_id: str | None = None,
    ) -> bool:
        """Persist a provider receipt, optionally atomically fenced to a live claim."""
        with self._db:
            if operation_id is None or worker_id is None:
                self._db.execute(
                    "INSERT OR IGNORE INTO memory_projection_bindings "
                    "(target,memory_id,version,external_id) VALUES(?,?,?,?)",
                    (target, memory_id, version, external_id),
                )
                return True
            cur = self._db.execute(
                "INSERT OR IGNORE INTO memory_projection_bindings(target,memory_id,version,external_id) "
                "SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM memory_projection_outbox "
                "WHERE operation_id=? AND state='running' AND worker_id=? "
                "AND lease_until IS NOT NULL AND lease_until>?)",
                (target, memory_id, version, external_id, operation_id, worker_id, self._clock()),
            )
        return cur.rowcount > 0 or self.projection_binding(target, memory_id, version) is not None

    def close(self) -> None:
        self._db.close()
