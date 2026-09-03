"""Fail-closed, content-free durable state for secondary channel turns."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

PENDING, PROCESSING, SENDING, CONFIRMED, REQUIRES_REVIEW = (
    "pending", "processing", "sending", "confirmed", "requires_review",
)
_PROVIDERS = frozenset({"slack", "discord", "email"})


class SecondaryChannelTurnStore:
    """A durable idempotency fence. It stores no messages, addresses, or raw IDs."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS secondary_channel_turns(
          event_fingerprint TEXT PRIMARY KEY, provider TEXT NOT NULL, state TEXT NOT NULL,
          worker_id TEXT, lease_until REAL, receipt_fingerprint TEXT NOT NULL DEFAULT '',
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS secondary_channel_turns_state ON secondary_channel_turns(state, lease_until);
        """)
        self._db.commit()

    def accept(self, *, provider: str, event_id: str) -> bool:
        provider = _provider(provider)
        if not event_id:
            return False
        now = self._clock()
        cur = self._db.execute(
            "INSERT OR IGNORE INTO secondary_channel_turns VALUES(?,?,?,?,?,?,?,?)",
            (_fingerprint(provider, event_id), provider, PENDING, None, None, "", now, now),
        )
        self._db.commit()
        return cur.rowcount == 1

    def claim_processing(self, *, provider: str, event_id: str, worker_id: str | None = None) -> str | None:
        provider, worker = _provider(provider), worker_id or uuid.uuid4().hex
        key, now = _fingerprint(provider, event_id), self._clock()
        cur = self._db.execute(
            "UPDATE secondary_channel_turns SET state=?,worker_id=?,lease_until=?,updated_ts=? "
            "WHERE event_fingerprint=? AND state=?", (PROCESSING, worker, now + 300, now, key, PENDING),
        )
        self._db.commit()
        return worker if cur.rowcount == 1 else None

    def state(self, *, provider: str, event_id: str) -> str | None:
        row = self._db.execute("SELECT state FROM secondary_channel_turns WHERE event_fingerprint=?",
                               (_fingerprint(_provider(provider), event_id),)).fetchone()
        return str(row["state"]) if row else None

    def begin_delivery(self, *, provider: str, event_id: str, worker_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE secondary_channel_turns SET state=?,lease_until=?,updated_ts=? "
            "WHERE event_fingerprint=? AND state=? AND worker_id=?",
            (SENDING, self._clock() + 60, self._clock(), _fingerprint(_provider(provider), event_id), PROCESSING, worker_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    def confirm_delivery(self, *, provider: str, event_id: str, worker_id: str, receipt: str) -> bool:
        if not receipt:
            return False
        cur = self._db.execute(
            "UPDATE secondary_channel_turns SET state=?,worker_id=NULL,lease_until=NULL,receipt_fingerprint=?,updated_ts=? "
            "WHERE event_fingerprint=? AND state=? AND worker_id=?",
            (CONFIRMED, hashlib.sha256(receipt.encode()).hexdigest(), self._clock(),
             _fingerprint(_provider(provider), event_id), SENDING, worker_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    def require_review(self, *, provider: str, event_id: str, worker_id: str | None = None) -> bool:
        clause, params = (" AND worker_id=?", [worker_id]) if worker_id else ("", [])
        cur = self._db.execute(
            "UPDATE secondary_channel_turns SET state=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE event_fingerprint=? AND state IN (?,?)" + clause,
            [REQUIRES_REVIEW, self._clock(), _fingerprint(_provider(provider), event_id), PROCESSING, SENDING, *params],
        )
        self._db.commit()
        return cur.rowcount == 1

    def recover_after_restart(self) -> int:
        cur = self._db.execute("UPDATE secondary_channel_turns SET state=?,worker_id=NULL,lease_until=NULL,updated_ts=? WHERE state IN (?,?,?)", (REQUIRES_REVIEW, self._clock(), PENDING, PROCESSING, SENDING))
        self._db.commit()
        return cur.rowcount

    def summary(self) -> dict[str, int]:
        states = (PENDING, PROCESSING, SENDING, CONFIRMED, REQUIRES_REVIEW)
        counts = {state: 0 for state in states}
        for row in self._db.execute("SELECT state,COUNT(*) count FROM secondary_channel_turns GROUP BY state"):
            if row["state"] in counts:
                counts[row["state"]] = int(row["count"])
        return {**counts, "total": sum(counts.values()), "open": counts[PENDING] + counts[PROCESSING] + counts[SENDING], "requires_owner_review": counts[REQUIRES_REVIEW]}

    def close(self) -> None: self._db.close()

def _provider(value: str) -> str:
    if value not in _PROVIDERS:
        raise ValueError("unsupported secondary provider")
    return value
def _fingerprint(provider: str, event_id: str) -> str:
    return hashlib.sha256(f"{provider}:{event_id}".encode()).hexdigest()
