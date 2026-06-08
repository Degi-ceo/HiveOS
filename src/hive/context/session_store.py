"""
session_store.py — SQLite + FTS5 conversation store (Hermes SessionDB).

Ported/adapted from Hermes hermes_state.SessionDB (docs/references/HERMES_REFERENCE.md
§"hermes_state"): durable sessions + messages with full-text recall, plus the
byte-exact system-prompt slot that powers prefix-cache reuse (prompt_builder).
Replaces the old Core/session.py. SQLite-first (OpenClaw rule); runtime reads the
canonical shape only. Deterministic status aging (active -> stale -> archived) is
maintenance, not runtime branching.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from hive.core.types import Message, Role

_STALE_AFTER = 30 * 86_400.0
_ARCHIVE_AFTER = 90 * 86_400.0


class SessionStore:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, created REAL, updated REAL,
              status TEXT DEFAULT 'active', system_prompt TEXT, summary TEXT);
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL,
              role TEXT, content TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(content, content='messages', content_rowid='id');
            """
        )
        self._db.commit()

    def ensure(self, session_id: str) -> None:
        now = self._clock()
        self._db.execute(
            "INSERT OR IGNORE INTO sessions(id, created, updated) VALUES(?,?,?)",
            (session_id, now, now),
        )
        self._db.commit()

    def append(self, session_id: str, role: Role | str, content: str) -> int:
        self.ensure(session_id)
        role_val = role.value if isinstance(role, Role) else role
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO messages(session, ts, role, content) VALUES(?,?,?,?)",
            (session_id, now, role_val, content),
        )
        self._db.execute(
            "INSERT INTO messages_fts(rowid, content) VALUES(?,?)", (cur.lastrowid, content)
        )
        self._db.execute("UPDATE sessions SET updated=? WHERE id=?", (now, session_id))
        self._db.commit()
        return int(cur.lastrowid)

    def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        sql = "SELECT role, content FROM messages WHERE session=? ORDER BY id"
        params: tuple = (session_id,)
        if limit is not None:
            sql = ("SELECT role, content FROM ("
                   "SELECT id, role, content FROM messages WHERE session=? "
                   "ORDER BY id DESC LIMIT ?) ORDER BY id")
            params = (session_id, limit)
        rows = self._db.execute(sql, params).fetchall()
        return [Message(role=Role(r["role"]), content=r["content"]) for r in rows]

    def search(self, query: str, *, session_id: str | None = None, limit: int = 10) -> list[dict]:
        sql = ("SELECT m.session, m.role, m.content FROM messages_fts f "
               "JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH ?")
        params: list = [query]
        if session_id is not None:
            sql += " AND m.session=?"
            params.append(session_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # --- prefix-cache slot + summary -------------------------------------------

    def save_system_prompt(self, session_id: str, text: str) -> None:
        self.ensure(session_id)
        self._db.execute("UPDATE sessions SET system_prompt=? WHERE id=?", (text, session_id))
        self._db.commit()

    def get_system_prompt(self, session_id: str) -> str | None:
        row = self._db.execute(
            "SELECT system_prompt FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row["system_prompt"] if row else None

    def update_summary(self, session_id: str, summary: str) -> None:
        self.ensure(session_id)
        self._db.execute("UPDATE sessions SET summary=? WHERE id=?", (summary, session_id))
        self._db.commit()

    def get_summary(self, session_id: str) -> str | None:
        row = self._db.execute(
            "SELECT summary FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row["summary"] if row else None

    def sweep(self) -> dict[str, int]:
        """Deterministic aging: active -> stale (30d idle) -> archived (90d idle)."""
        now = self._clock()
        stale = self._db.execute(
            "UPDATE sessions SET status='stale' WHERE status='active' AND updated < ?",
            (now - _STALE_AFTER,),
        ).rowcount
        archived = self._db.execute(
            "UPDATE sessions SET status='archived' WHERE status!='archived' AND updated < ?",
            (now - _ARCHIVE_AFTER,),
        ).rowcount
        self._db.commit()
        return {"stale": stale, "archived": archived}

    def close(self) -> None:
        self._db.close()
