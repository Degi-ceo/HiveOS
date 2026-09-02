"""Durable, conservative inbox for Telegram webhook deliveries.

Ingress is deduplicated by a non-secret bot scope and Telegram update id.  It never
replays a model turn or possibly delivered reply after a crash: such records become
``ambiguous`` for an operator instead of creating duplicate side effects.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PENDING = "pending"
PROCESSING = "processing"
REPLY_PENDING = "reply_pending"
SENDING = "sending"
REPLIED = "replied"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_id: int
    state: str
    session_id: str
    chat_id: str
    user_id: str
    message_id: str
    thread_id: str
    reply_text: str | None
    worker_id: str | None
    lease_until: float | None
    receipt_fingerprint: str = ""


class TelegramInbox:
    """SQLite-owned Telegram delivery state; no task board or memory coupling."""

    def __init__(self, db_path: str | Path, *, bot_scope: str = "default",
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._scope = bot_scope
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_updates(
          bot_scope TEXT NOT NULL,
          update_id INTEGER NOT NULL,
          session_id TEXT NOT NULL,
          chat_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          message_id TEXT NOT NULL DEFAULT '',
          thread_id TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL,
          reply_text TEXT,
          worker_id TEXT,
          lease_until REAL,
          receipt_fingerprint TEXT NOT NULL DEFAULT '',
          created_ts REAL NOT NULL,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(bot_scope, update_id));
        CREATE INDEX IF NOT EXISTS telegram_updates_state
          ON telegram_updates(state, lease_until);
        """)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(telegram_updates)")}
        if "receipt_fingerprint" not in columns:
            self._db.execute("ALTER TABLE telegram_updates ADD COLUMN receipt_fingerprint TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def accept(self, *, update_id: int, session_id: str, chat_id: str, user_id: str,
               message_id: str, thread_id: str) -> bool:
        """Persist an authenticated update once; its text is intentionally not stored."""
        now = self._clock()
        cur = self._db.execute(
            "INSERT OR IGNORE INTO telegram_updates("
            "bot_scope,update_id,session_id,chat_id,user_id,message_id,thread_id,state,created_ts,updated_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self._scope, update_id, session_id, chat_id, user_id, message_id, thread_id,
             PENDING, now, now),
        )
        self._db.commit()
        return cur.rowcount == 1

    def claim_processing(self, update_id: int, *, worker_id: str | None = None,
                         lease_seconds: float = 300.0) -> bool:
        worker = worker_id or uuid.uuid4().hex
        now = self._clock()
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,worker_id=?,lease_until=?,updated_ts=? "
            "WHERE bot_scope=? AND update_id=? AND state=?",
            (PROCESSING, worker, now + max(1.0, lease_seconds), now,
             self._scope, update_id, PENDING),
        )
        self._db.commit()
        return cur.rowcount == 1

    def store_reply(self, update_id: int, *, worker_id: str, reply_text: str) -> bool:
        """Durably save the completed model response before delivery."""
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,reply_text=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE bot_scope=? AND update_id=? AND state=? AND worker_id=?",
            (REPLY_PENDING, reply_text, self._clock(), self._scope, update_id,
             PROCESSING, worker_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    def claim_send(self, update_id: int, *, worker_id: str | None = None,
                   lease_seconds: float = 60.0) -> TelegramUpdate | None:
        worker = worker_id or uuid.uuid4().hex
        now = self._clock()
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,worker_id=?,lease_until=?,updated_ts=? "
            "WHERE bot_scope=? AND update_id=? AND state=?",
            (SENDING, worker, now + max(1.0, lease_seconds), now,
             self._scope, update_id, REPLY_PENDING),
        )
        self._db.commit()
        return self.get(update_id) if cur.rowcount == 1 else None

    def mark_replied(self, update_id: int, *, worker_id: str, receipt: str) -> bool:
        """Terminalize only after a non-empty provider receipt is available."""
        if not receipt:
            return False
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,receipt_fingerprint=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE bot_scope=? AND update_id=? AND state=? AND worker_id=?",
            (REPLIED, hashlib.sha256(receipt.encode("utf-8")).hexdigest(), self._clock(),
             self._scope, update_id, SENDING, worker_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    def mark_ambiguous(self, update_id: int, *, worker_id: str | None = None) -> bool:
        return self._transition(update_id, AMBIGUOUS, worker_id)

    def recover_expired(self, now: float | None = None) -> int:
        """Quarantine expired in-flight work; replay could duplicate external effects."""
        cutoff = self._clock() if now is None else now
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE bot_scope=? AND state IN (?,?) AND lease_until IS NOT NULL AND lease_until<=?",
            (AMBIGUOUS, cutoff, self._scope, PROCESSING, SENDING, cutoff),
        )
        self._db.commit()
        return cur.rowcount

    def recover_after_restart(self) -> int:
        """Fail closed after a gateway restart without replaying an unfinished turn.

        A live lease only proves ownership inside the process that acquired it.  Once
        that process has stopped, neither a still-valid processing lease nor a
        ``reply_pending`` record proves whether a subsequent reply attempt is safe.
        Quarantine every non-terminal stage. Even ``pending`` is not resumed: a
        restart cannot prove whether another worker began processing between the
        durable insert and its state transition. An operator may inspect aggregate
        health, but this method deliberately never resumes, sends, or exposes an
        update.
        """
        now = self._clock()
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE bot_scope=? AND state IN (?,?,?,?)",
            (AMBIGUOUS, now, self._scope, PENDING, PROCESSING, REPLY_PENDING, SENDING),
        )
        self._db.commit()
        return cur.rowcount

    def summary(self) -> dict[str, int]:
        """Return aggregate-only inbox health suitable for an operator surface.

        The result intentionally contains no update, chat, user, session, reply, or
        error data.  ``open`` means a non-terminal record is still present; after a
        startup recovery this should be zero.  ``requires_review`` counts quarantined
        records and never implies that an outbound retry is safe.
        """
        known_states = (PENDING, PROCESSING, REPLY_PENDING, SENDING, REPLIED, AMBIGUOUS)
        counts = {state: 0 for state in known_states}
        unknown = 0
        for row in self._db.execute(
            "SELECT state, COUNT(*) AS count FROM telegram_updates "
            "WHERE bot_scope=? GROUP BY state", (self._scope,)
        ):
            state, count = str(row["state"]), int(row["count"])
            if state in counts:
                counts[state] = count
            else:
                unknown += count
        return {
            **counts,
            "unknown": unknown,
            "total": sum(counts.values()) + unknown,
            "open": counts[PENDING] + counts[PROCESSING] + counts[REPLY_PENDING] + counts[SENDING] + unknown,
            "requires_review": counts[AMBIGUOUS],
        }

    def get(self, update_id: int) -> TelegramUpdate | None:
        row = self._db.execute(
            "SELECT * FROM telegram_updates WHERE bot_scope=? AND update_id=?",
            (self._scope, update_id),
        ).fetchone()
        return _row(row) if row else None

    def close(self) -> None:
        self._db.close()

    def _transition(self, update_id: int, state: str, worker_id: str | None) -> bool:
        clauses = "AND worker_id=?" if worker_id is not None else ""
        params: list[object] = [state, self._clock(), self._scope, update_id]
        if worker_id is not None:
            params.append(worker_id)
        cur = self._db.execute(
            "UPDATE telegram_updates SET state=?,worker_id=NULL,lease_until=NULL,updated_ts=? "
            "WHERE bot_scope=? AND update_id=? " + clauses,
            params,
        )
        self._db.commit()
        return cur.rowcount == 1


def _row(row: sqlite3.Row) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=int(row["update_id"]), state=str(row["state"]),
        session_id=str(row["session_id"]), chat_id=str(row["chat_id"]),
        user_id=str(row["user_id"]), message_id=str(row["message_id"]),
        thread_id=str(row["thread_id"]), reply_text=row["reply_text"],
        worker_id=row["worker_id"], lease_until=row["lease_until"],
        receipt_fingerprint=str(row["receipt_fingerprint"] or ""),
    )
