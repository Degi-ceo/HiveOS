"""Durable, conservative inbox for Telegram webhook deliveries.

Ingress is deduplicated by a non-secret bot scope and Telegram update id.  It never
replays a model turn or possibly delivered reply after a crash: such records become
``ambiguous`` for an operator instead of creating duplicate side effects.
"""
from __future__ import annotations

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
          created_ts REAL NOT NULL,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(bot_scope, update_id));
        CREATE INDEX IF NOT EXISTS telegram_updates_state
          ON telegram_updates(state, lease_until);
        """)
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

    def mark_replied(self, update_id: int, *, worker_id: str) -> bool:
        return self._transition(update_id, REPLIED, worker_id)

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
    )
