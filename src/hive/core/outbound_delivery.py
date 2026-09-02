"""Durable, privacy-preserving evidence for outbound provider effects.

This is deliberately *not* a retry queue.  It records only non-content
metadata around an already-authorized send.  Any interrupted or receipt-less
attempt is quarantined for owner review rather than replayed.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

PENDING = "pending"
IN_FLIGHT = "in_flight"
CONFIRMED = "confirmed"
REQUIRES_REVIEW = "requires_review"


class OutboundDeliveryLedger:
    """Append-only, aggregate-safe send evidence; never stores content or recipients."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS outbound_delivery_effects(
          correlation_id TEXT PRIMARY KEY,
          surface TEXT NOT NULL,
          provider TEXT NOT NULL,
          state TEXT NOT NULL,
          receipt_fingerprint TEXT NOT NULL DEFAULT '',
          created_ts REAL NOT NULL,
          updated_ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outbound_delivery_effects_state
          ON outbound_delivery_effects(state);
        """)
        self._db.commit()

    def begin(self, *, surface: str, provider: str) -> str:
        """Persist intent before a provider call; caller must never replay it."""
        correlation_id = uuid.uuid4().hex
        now = self._clock()
        self._db.execute(
            "INSERT INTO outbound_delivery_effects VALUES(?,?,?,?,?,?,?)",
            (correlation_id, _fixed(surface), _fixed(provider), IN_FLIGHT, "", now, now),
        )
        self._db.commit()
        return correlation_id

    def confirm(self, correlation_id: str, *, receipt: str) -> bool:
        """Record only a one-way fingerprint of a provider acceptance receipt."""
        if not receipt:
            return False
        fingerprint = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        cur = self._db.execute(
            "UPDATE outbound_delivery_effects SET state=?,receipt_fingerprint=?,updated_ts=? "
            "WHERE correlation_id=? AND state=?",
            (CONFIRMED, fingerprint, self._clock(), correlation_id, IN_FLIGHT),
        )
        self._db.commit()
        return cur.rowcount == 1

    def require_review(self, correlation_id: str) -> bool:
        """Quarantine an unconfirmed send.  This method never sends or retries."""
        cur = self._db.execute(
            "UPDATE outbound_delivery_effects SET state=?,updated_ts=? "
            "WHERE correlation_id=? AND state IN (?,?)",
            (REQUIRES_REVIEW, self._clock(), correlation_id, PENDING, IN_FLIGHT),
        )
        self._db.commit()
        return cur.rowcount == 1

    def recover_after_restart(self) -> int:
        """Fence interrupted calls: after a restart their external result is unknowable."""
        cur = self._db.execute(
            "UPDATE outbound_delivery_effects SET state=?,updated_ts=? WHERE state IN (?,?)",
            (REQUIRES_REVIEW, self._clock(), PENDING, IN_FLIGHT),
        )
        self._db.commit()
        return cur.rowcount

    def summary(self) -> dict[str, int]:
        states = (PENDING, IN_FLIGHT, CONFIRMED, REQUIRES_REVIEW)
        counts = {state: 0 for state in states}
        unknown = 0
        for row in self._db.execute("SELECT state,COUNT(*) count FROM outbound_delivery_effects GROUP BY state"):
            state, count = str(row["state"]), int(row["count"])
            if state in counts:
                counts[state] = count
            else:
                unknown += count
        return {**counts, "unknown": unknown, "total": sum(counts.values()) + unknown,
                "open": counts[PENDING] + counts[IN_FLIGHT] + unknown,
                "requires_owner_review": counts[REQUIRES_REVIEW] + unknown}

    def close(self) -> None:
        self._db.close()


def _fixed(value: str) -> str:
    """Bound metadata in case an integration supplies an unexpected provider label."""
    return str(value).lower()[:32]
