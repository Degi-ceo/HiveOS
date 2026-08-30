"""Canonical versioned memory ledger; projections are derived, never authoritative."""
from __future__ import annotations

import hashlib
import json
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
        CREATE TABLE IF NOT EXISTS memory_versions(memory_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL, created_ts REAL NOT NULL, PRIMARY KEY(memory_id,version));
        CREATE TABLE IF NOT EXISTS memory_events(event_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_projection_outbox(operation_id TEXT PRIMARY KEY, target TEXT NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, operation TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL, UNIQUE(target,memory_id,version,operation));
        """)
        self._db.commit()

    def remember(self, *, kind: str, stable_key: str, content: str, source: str, idempotency_key: str, targets: tuple[str, ...] = ("mnemosyne", "obsidian")) -> MemoryVersion:
        if not stable_key.strip() or not content.strip() or not idempotency_key.strip():
            raise ValueError("stable_key, content and idempotency_key are required")
        now = self._clock()
        with self._db:
            duplicate = self._db.execute("SELECT memory_id FROM memory_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if duplicate:
                return self.get_current(duplicate["memory_id"])
            prior = self._db.execute("SELECT memory_id,current_version FROM memory_items WHERE stable_key=?", (stable_key,)).fetchone()
            memory_id = prior["memory_id"] if prior else f"mem_{uuid.uuid4().hex}"
            version = int(prior["current_version"]) + 1 if prior else 1
            digest = hashlib.sha256(content.encode()).hexdigest()
            if prior:
                self._db.execute("UPDATE memory_items SET kind=?,status='active',current_version=?,updated_ts=? WHERE memory_id=?", (kind, version, now, memory_id))
            else:
                self._db.execute("INSERT INTO memory_items VALUES(?,?,?,?,?,?,?)", (memory_id, stable_key, kind, "active", version, now, now))
            self._db.execute("INSERT INTO memory_versions VALUES(?,?,?,?,?,?)", (memory_id, version, content, source, digest, now))
            self._db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", idempotency_key, memory_id, version, "created" if version == 1 else "superseded", json.dumps({"source": source}), now))
            for target in targets:
                self._db.execute("INSERT INTO memory_projection_outbox(operation_id,target,memory_id,version,operation,created_ts) VALUES(?,?,?,?,?,?)", (f"proj_{uuid.uuid4().hex}", target, memory_id, version, "upsert", now))
        return self.get_current(memory_id)

    def get_current(self, memory_id: str) -> MemoryVersion:
        row = self._db.execute("SELECT i.memory_id,i.current_version AS version,i.kind,i.stable_key,v.content,v.source,i.status,v.content_hash FROM memory_items i JOIN memory_versions v ON v.memory_id=i.memory_id AND v.version=i.current_version WHERE i.memory_id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return MemoryVersion(**dict(row))

    def get_version(self, memory_id: str, version: int) -> MemoryVersion:
        row = self._db.execute(
            "SELECT i.memory_id,v.version,i.kind,i.stable_key,v.content,v.source,"
            "i.status,v.content_hash FROM memory_items i JOIN memory_versions v "
            "ON v.memory_id=i.memory_id WHERE i.memory_id=? AND v.version=?",
            (memory_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"{memory_id}@{version}")
        return MemoryVersion(**dict(row))

    def pending_projections(self, target: str) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM memory_projection_outbox WHERE target=? AND state='pending' ORDER BY created_ts", (target,))]

    def mark_projected(self, operation_id: str) -> None:
        with self._db:
            self._db.execute("UPDATE memory_projection_outbox SET state='applied',attempts=attempts+1 WHERE operation_id=? AND state='pending'", (operation_id,))

    def record_projection_failure(self, operation_id: str) -> None:
        """Keep the operation pending while recording that it needs reconciliation."""
        with self._db:
            self._db.execute(
                "UPDATE memory_projection_outbox SET attempts=attempts+1 "
                "WHERE operation_id=? AND state='pending'",
                (operation_id,),
            )

    def close(self) -> None:
        self._db.close()
