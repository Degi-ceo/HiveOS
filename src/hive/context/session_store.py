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

import hashlib
import hmac
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from hive.core.types import Message, Role

log = logging.getLogger("hive.context.session_store")

_STALE_AFTER = 30 * 86_400.0
_ARCHIVE_AFTER = 90 * 86_400.0
_SURFACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def opaque_subject_id(surface: str, subject: str, secret: str) -> str:
    """Return a stable, non-reversible key for a channel subject.

    Channel identifiers such as Telegram chat IDs and email addresses are
    personal data.  Session links need a stable join key, but must not put
    that identifier in the shared state database.  Callers retain the raw
    subject only long enough to derive this HMAC value.
    """
    normalized_surface = str(surface).strip().casefold()
    normalized_subject = str(subject).strip()
    if not _SURFACE_RE.fullmatch(normalized_surface):
        raise ValueError("invalid channel surface")
    if not normalized_subject or len(normalized_subject) > 512:
        raise ValueError("invalid channel subject")
    key = str(secret).encode("utf-8")
    if not key:
        raise ValueError("session-link secret must not be empty")
    payload = f"hiveos-session-link-v1\\0{normalized_surface}\\0{normalized_subject}".encode("utf-8")
    return "v1:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def _coerce_role(value: str) -> Role:
    """Map a stored role string to Role, defaulting unknowns to USER (never raise)."""
    try:
        return Role(value)
    except ValueError:
        return Role.USER


class SessionStore:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")  # shared state DB: reduce writer lock contention
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, created REAL, updated REAL,
              status TEXT DEFAULT 'active', system_prompt TEXT, summary TEXT,
              title TEXT);
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL,
              role TEXT, content TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(content, content='messages', content_rowid='id');
            CREATE TABLE IF NOT EXISTS session_links(
              surface TEXT NOT NULL,
              subject_key TEXT NOT NULL,
              session_id TEXT NOT NULL,
              created REAL NOT NULL,
              updated REAL NOT NULL,
              PRIMARY KEY(surface, subject_key)
            );
            CREATE INDEX IF NOT EXISTS idx_session_links_session
              ON session_links(session_id, updated DESC);
            """
        )
        # Idempotent column add for DBs created before `title` existed (B3).
        try:
            self._db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        self._db.commit()

    def ensure(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
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
        if cur.lastrowid is None:
            self._db.rollback()
            raise RuntimeError("insert did not produce a row id")
        row_id = int(cur.lastrowid)
        self._db.execute(
            "INSERT INTO messages_fts(rowid, content) VALUES(?,?)", (row_id, content)
        )
        self._db.execute("UPDATE sessions SET updated=? WHERE id=?", (now, session_id))
        self._db.commit()
        return row_id

    def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        sql = "SELECT role, content FROM messages WHERE session=? ORDER BY id"
        params: tuple = (session_id,)
        if limit is not None:
            sql = ("SELECT role, content FROM ("
                   "SELECT id, role, content FROM messages WHERE session=? "
                   "ORDER BY id DESC LIMIT ?) ORDER BY id")
            params = (session_id, limit)
        rows = self._db.execute(sql, params).fetchall()
        # Tolerant role read: a stray/out-of-enum role row must not brick the whole session.
        return [Message(role=_coerce_role(r["role"]), content=r["content"]) for r in rows]

    def message_details(self, session_id: str, *, limit: int = 100) -> list[dict]:
        """Return bounded transcript rows for an authenticated local operator.

        Callers must redact content before display.  Raw channel subjects are
        never part of a message row, and this method intentionally does not
        join the session-link table.
        """
        safe_limit = max(1, min(int(limit), 500))
        try:
            rows = self._db.execute(
                "SELECT id, ts, role, content FROM ("
                "SELECT id, ts, role, content FROM messages WHERE session=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (str(session_id), safe_limit),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("message_details failed: %s", exc)
            return []

    def search(self, query: str, *, session_id: str | None = None, limit: int = 10) -> list[dict]:
        sql = ("SELECT m.session, m.role, m.content FROM messages_fts f "
               "JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH ?")
        params: list = [query]
        if session_id is not None:
            sql += " AND m.session=?"
            params.append(session_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            # Raw user text isn't valid FTS5 syntax (quotes, "or", "-", "NEAR(") — fall
            # back to LIKE, mirroring local.recall, so search never crashes on chat input.
            like = "SELECT session, role, content FROM messages WHERE content LIKE ?"
            like_params: list = [f"%{query}%"]
            if session_id is not None:
                like += " AND session=?"
                like_params.append(session_id)
            like += " ORDER BY id DESC LIMIT ?"
            like_params.append(limit)
            return [dict(r) for r in self._db.execute(like, like_params).fetchall()]

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

    def set_title(self, session_id: str, title: str) -> None:
        self.ensure(session_id)
        self._db.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
        self._db.commit()

    def get_title(self, session_id: str) -> str | None:
        row = self._db.execute(
            "SELECT title FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row["title"] if row else None

    def list_sessions(self) -> list[str]:
        """Return all known session IDs ordered by last update."""
        try:
            rows = self._db.execute(
                "SELECT id FROM sessions ORDER BY updated DESC"
            ).fetchall()
            return [r["id"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("list_sessions failed: %s", exc)
            return []

    def list_session_details(self, *, limit: int = 100) -> list[dict]:
        """List operator-safe session metadata without message contents.

        ``link_count`` exposes only the number of bound channel subjects; raw
        platform identifiers and their HMAC values deliberately never leave
        this store through the operator-facing API.
        """
        safe_limit = max(1, min(int(limit), 500))
        try:
            rows = self._db.execute(
                "SELECT s.id, s.created, s.updated, s.status, s.title, s.summary, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session=s.id) AS message_count, "
                "(SELECT COUNT(*) FROM session_links l WHERE l.session_id=s.id) AS link_count "
                "FROM sessions s ORDER BY s.updated DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("list_session_details failed: %s", exc)
            return []

    def bind_link(self, surface: str, subject_key: str, session_id: str) -> None:
        """Explicitly bind one opaque channel subject to one conversation.

        Rebinding is intentional and atomic: an operator can move one inbound
        channel identity to a different named conversation without copying or
        deleting its prior transcript.
        """
        normalized_surface = str(surface).strip().casefold()
        normalized_key = str(subject_key).strip()
        normalized_session = str(session_id).strip()
        if not _SURFACE_RE.fullmatch(normalized_surface):
            raise ValueError("invalid channel surface")
        if not re.fullmatch(r"v1:[0-9a-f]{64}", normalized_key):
            raise ValueError("invalid opaque channel subject")
        if not normalized_session or len(normalized_session) > 128:
            raise ValueError("invalid session id")
        now = self._clock()
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO sessions(id, created, updated) VALUES(?,?,?)",
                (normalized_session, now, now),
            )
            self._db.execute(
                "INSERT INTO session_links(surface, subject_key, session_id, created, updated) "
                "VALUES(?,?,?,?,?) ON CONFLICT(surface, subject_key) DO UPDATE SET "
                "session_id=excluded.session_id, updated=excluded.updated",
                (normalized_surface, normalized_key, normalized_session, now, now),
            )

    def resolve_link(self, surface: str, subject_key: str) -> str | None:
        """Resolve an opaque channel subject to an explicitly linked session."""
        normalized_surface = str(surface).strip().casefold()
        if not _SURFACE_RE.fullmatch(normalized_surface):
            return None
        try:
            row = self._db.execute(
                "SELECT session_id FROM session_links WHERE surface=? AND subject_key=?",
                (normalized_surface, str(subject_key).strip()),
            ).fetchone()
            return str(row["session_id"]) if row is not None else None
        except sqlite3.Error as exc:
            log.warning("resolve_link failed: %s", exc)
            return None

    def linked_surfaces(self, session_id: str) -> dict[str, int]:
        """Return aggregate link counts by surface, never individual subjects."""
        try:
            rows = self._db.execute(
                "SELECT surface, COUNT(*) AS count FROM session_links WHERE session_id=? "
                "GROUP BY surface ORDER BY surface",
                (str(session_id),),
            ).fetchall()
            return {str(row["surface"]): int(row["count"]) for row in rows}
        except sqlite3.Error as exc:
            log.warning("linked_surfaces failed: %s", exc)
            return {}

    def link_details(self, session_id: str, *, limit: int = 100) -> list[dict]:
        """Return operator-safe channel links with non-reversible short refs."""
        safe_limit = max(1, min(int(limit), 500))
        try:
            rows = self._db.execute(
                "SELECT surface, subject_key, created, updated FROM session_links "
                "WHERE session_id=? ORDER BY updated DESC LIMIT ?",
                (str(session_id), safe_limit),
            ).fetchall()
            return [
                {"surface": str(row["surface"]), "ref": str(row["subject_key"])[3:15],
                 "created": float(row["created"]), "updated": float(row["updated"])}
                for row in rows
            ]
        except sqlite3.Error as exc:
            log.warning("link_details failed: %s", exc)
            return []

    def delete_session(self, session_id: str) -> int:
        """Delete all messages and session record for a session. Returns messages deleted."""
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM messages WHERE session=?", (session_id,)
                )
                self._db.execute("DELETE FROM session_links WHERE session_id=?", (session_id,))
                self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return cur.rowcount
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_session failed: %s", exc)
            return 0

    def count_messages(self, session_id: str) -> int:
        """Return the number of messages stored for the given session."""
        try:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session=?", (session_id,)
            ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error as exc:
            log.warning("count_messages failed: %s", exc)
            return 0

    def stats(self) -> dict:
        """Return aggregate statistics across all sessions."""
        try:
            session_count = self._db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            message_count = self._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            status_rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["n"] for r in status_rows}
        except sqlite3.Error as exc:
            log.warning("stats failed: %s", exc)
            return {"sessions": 0, "messages": 0, "by_status": {}}
        return {"sessions": int(session_count), "messages": int(message_count),
                "by_status": by_status}

    def delete_archived(self, max_age_days: float = 90) -> int:
        """Delete sessions that have been archived for more than max_age_days.
        Returns the number of sessions deleted."""
        cutoff = self._clock() - max_age_days * 86_400
        try:
            ids_to_delete = [
                r["id"] for r in self._db.execute(
                    "SELECT id FROM sessions WHERE status='archived' AND updated < ?",
                    (cutoff,)
                ).fetchall()
            ]
            deleted = 0
            for sid in ids_to_delete:
                deleted += self.delete_session(sid)
            return len(ids_to_delete)
        except sqlite3.Error as exc:
            log.warning("delete_archived failed: %s", exc)
            return 0

    def session_count(self) -> int:
        """Return the total number of sessions (all statuses)."""
        try:
            row = self._db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
            return int(row["n"]) if row else 0
        except Exception as exc:  # noqa: BLE001
            log.warning("session_count failed: %s", exc)
            return 0

    def total_message_count(self) -> int:
        """Return the total number of messages stored across all sessions."""
        try:
            row = self._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            return int(row["n"]) if row else 0
        except Exception as exc:  # noqa: BLE001
            log.warning("total_message_count failed: %s", exc)
            return 0

    def oldest_session(self) -> str | None:
        """Return the session_id of the oldest session (by creation time), or None."""
        try:
            row = self._db.execute(
                "SELECT id FROM sessions ORDER BY created ASC LIMIT 1"
            ).fetchone()
            return row["id"] if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("oldest_session failed: %s", exc)
            return None

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
