"""Durable Telegram-to-Hive session selection without destructive resets."""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class TelegramSession:
    session_id: str
    active: bool
    created_ts: float
    updated_ts: float


class TelegramSessionBindings:
    """Own the active Hive session for each authenticated Telegram conversation."""

    def __init__(self, db_path: str | Path, *, bot_scope: str = "telegram",
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._scope = bot_scope
        self._clock = clock
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_session_bindings(
          bot_scope TEXT NOT NULL,
          chat_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          thread_id TEXT NOT NULL DEFAULT '',
          session_id TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 0,
          created_ts REAL NOT NULL,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(bot_scope, chat_id, user_id, thread_id, session_id));
        CREATE UNIQUE INDEX IF NOT EXISTS telegram_session_one_active
          ON telegram_session_bindings(bot_scope, chat_id, user_id, thread_id)
          WHERE active=1;
        CREATE INDEX IF NOT EXISTS telegram_session_recent
          ON telegram_session_bindings(bot_scope, chat_id, user_id, thread_id, updated_ts DESC);
        """)
        self._db.commit()

    def active_or_create(self, *, chat_id: str, user_id: str, thread_id: str,
                         legacy_session_id: str) -> str:
        row = self._active(chat_id, user_id, thread_id)
        if row is not None:
            self._touch(str(row["session_id"]))
            return str(row["session_id"])
        now = self._clock()
        self._db.execute(
            "INSERT OR IGNORE INTO telegram_session_bindings("
            "bot_scope,chat_id,user_id,thread_id,session_id,active,created_ts,updated_ts) "
            "VALUES(?,?,?,?,?,1,?,?)",
            (self._scope, chat_id, user_id, thread_id, legacy_session_id, now, now),
        )
        self._db.commit()
        row = self._active(chat_id, user_id, thread_id)
        if row is None:
            raise RuntimeError("telegram session binding was not created")
        return str(row["session_id"])

    def new_session(self, *, chat_id: str, user_id: str, thread_id: str,
                    legacy_session_id: str) -> str:
        self.active_or_create(chat_id=chat_id, user_id=user_id, thread_id=thread_id,
                              legacy_session_id=legacy_session_id)
        now = self._clock()
        session_id = f"{legacy_session_id}:session:{uuid.uuid4().hex[:12]}"
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE telegram_session_bindings SET active=0,updated_ts=? "
                "WHERE bot_scope=? AND chat_id=? AND user_id=? AND thread_id=? AND active=1",
                (now, self._scope, chat_id, user_id, thread_id),
            )
            self._db.execute(
                "INSERT INTO telegram_session_bindings("
                "bot_scope,chat_id,user_id,thread_id,session_id,active,created_ts,updated_ts) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (self._scope, chat_id, user_id, thread_id, session_id, now, now),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return session_id

    def sessions(self, *, chat_id: str, user_id: str, thread_id: str) -> list[TelegramSession]:
        rows = self._db.execute(
            "SELECT session_id,active,created_ts,updated_ts FROM telegram_session_bindings "
            "WHERE bot_scope=? AND chat_id=? AND user_id=? AND thread_id=? "
            "ORDER BY active DESC,updated_ts DESC",
            (self._scope, chat_id, user_id, thread_id),
        ).fetchall()
        return [TelegramSession(session_id=str(row["session_id"]), active=bool(row["active"]),
                                created_ts=float(row["created_ts"]), updated_ts=float(row["updated_ts"]))
                for row in rows]

    def resume(self, *, chat_id: str, user_id: str, thread_id: str, index: int) -> str | None:
        choices = self.sessions(chat_id=chat_id, user_id=user_id, thread_id=thread_id)
        if index < 1 or index > len(choices):
            return None
        target = choices[index - 1].session_id
        now = self._clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE telegram_session_bindings SET active=0,updated_ts=? "
                "WHERE bot_scope=? AND chat_id=? AND user_id=? AND thread_id=? AND active=1",
                (now, self._scope, chat_id, user_id, thread_id),
            )
            self._db.execute(
                "UPDATE telegram_session_bindings SET active=1,updated_ts=? "
                "WHERE bot_scope=? AND chat_id=? AND user_id=? AND thread_id=? AND session_id=?",
                (now, self._scope, chat_id, user_id, thread_id, target),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return target

    def close(self) -> None:
        self._db.close()

    def _active(self, chat_id: str, user_id: str, thread_id: str):
        return self._db.execute(
            "SELECT session_id FROM telegram_session_bindings "
            "WHERE bot_scope=? AND chat_id=? AND user_id=? AND thread_id=? AND active=1",
            (self._scope, chat_id, user_id, thread_id),
        ).fetchone()

    def _touch(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE telegram_session_bindings SET updated_ts=? WHERE bot_scope=? AND session_id=?",
            (self._clock(), self._scope, session_id),
        )
        self._db.commit()
