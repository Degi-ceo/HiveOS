"""
local.py — LocalMemoryProvider: a working MemoryProvider backed by SQLite.

Ships real remember/recall "from day one" without external services (the local
fallback from the old Memory/brain.py), implementing the MemoryProvider contract.
The real Mnemosyne package is swapped in behind the SAME interface at P8
(memory/mnemosyne_provider.py) — callers never change.

SQLite-first (OpenClaw rule): durable knowledge + episodic turns live in the state
DB; FTS5 powers recall with a LIKE fallback when FTS is unavailable. Fail-open:
recall/prefetch/sync errors are logged, never raised — memory must not break a turn.
Durable learnings (skill/mcp/research/fix/fact) are also promoted to the Obsidian
vault, the long-term human-readable export.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.events import EventBus, EventType
from hive.memory.ledger import MemoryLedger
from hive.memory.obsidian_projector import ObsidianShadowProjector
from hive.memory.provider import MemoryProvider
from hive.memory.vault import ObsidianVault

log = logging.getLogger("hive.memory.local")

_PROMOTE_KINDS = ("skill", "mcp", "research", "fix", "fact")


class LocalMemoryProvider(MemoryProvider):
    name = "local"

    def __init__(
        self,
        db_path: str | Path,
        vault: ObsidianVault | None = None,
        *,
        ledger: MemoryLedger | None = None,
        shadow_root: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        bus: EventBus | None = None,
    ) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")  # shared state DB: reduce writer lock contention
        self._vault = vault
        self._ledger = ledger
        self._shadow = (ObsidianShadowProjector(ledger, shadow_root)
                        if ledger is not None and shadow_root is not None else None)
        self._clock = clock
        self._session = ""
        self._bus = bus
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS episodic(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session TEXT,
              role TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS knowledge(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,
              topic TEXT, content TEXT, source TEXT, importance REAL DEFAULT 0.5);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
              USING fts5(topic, content, source, content='knowledge', content_rowid='id');
            """
        )
        self._db.commit()

    # --- MemoryProvider contract ------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session = session_id

    def system_prompt_block(self) -> str:
        try:
            facts = self.most_important_facts(limit=5)
        except Exception:  # noqa: BLE001
            facts = []
        if not facts:
            return (
                "You have persistent memory. Use `recall` before researching or redoing "
                "work, and `remember` to save durable facts, fixes, and skills."
            )
        lines = [f"- [{f['kind']}] {f['topic']}: {f['content'][:120]}" for f in facts]
        return "## Persistent Memory (top facts)\n" + "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall block injected before a turn. Fail-open."""
        try:
            hits = self.recall(query, limit=5)
        except Exception as exc:  # noqa: BLE001 - memory must never break a turn
            log.warning("prefetch recall failed: %s", exc)
            return ""
        if not hits:
            return ""
        lines = [f"- [{h['kind']}] {h['topic']}: {h['content'][:200]}" for h in hits]
        return "## Recalled memory\n" + "\n".join(lines)

    def sync_turn(
        self, user_content: str, assistant_content: str,
        *, session_id: str = "", messages: list | None = None,
    ) -> None:
        """Log a completed turn to episodic memory. Fail-open."""
        session = session_id or self._session
        try:
            self._log_turn(session, "user", user_content)
            self._log_turn(session, "assistant", assistant_content)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_turn failed: %s", exc)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {
                "name": "remember",
                "description": "Save a durable memory or learning for later recall.",
                "parameters": {"type": "object", "properties": {
                    "content": {"type": "string", "description": "What to remember."},
                    "importance": {"type": "number", "description": "Salience 0..1."},
                }, "required": ["content"]}}},
            {"type": "function", "function": {
                "name": "recall",
                "description": "Search durable memory before doing or researching something.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                }, "required": ["query"]}}},
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "remember":
            self.remember(args["content"], importance=float(args.get("importance", 0.5)))
            return "Saved to memory."
        if tool_name == "recall":
            hits = self.recall(args["query"], limit=int(args.get("limit", 5)))
            if not hits:
                return "No relevant memory found."
            return "\n".join(f"[{h['kind']}] {h['topic']}: {h['content']}" for h in hits)
        return f"Unknown memory tool: {tool_name}"

    # --- direct API (used by the keeper and surfaces) ---------------------------

    def remember(self, content: str, *, importance: float = 0.5,
                 topic: str | None = None, source: str = "tool") -> None:
        resolved_topic = topic or content[:60]
        self._insert_knowledge("memory", resolved_topic, content, source, importance)
        self._record_canonical("memory", resolved_topic, content, source)
        if self._bus is not None:
            try:
                self._bus.publish(EventType.MEMORY_STORE,
                                  {"kind": "memory", "topic": topic or content[:60]})
            except Exception:  # noqa: BLE001
                pass

    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        """Persist a structured learning (skill|mcp|research|fix|fact) + promote to vault."""
        self._insert_knowledge(kind, topic, content, source, 0.7)
        self._record_canonical(kind, topic, content, source)
        if self._bus is not None:
            try:
                self._bus.publish(EventType.MEMORY_STORE, {"kind": kind, "topic": topic})
            except Exception:  # noqa: BLE001
                pass
        if self._ledger is None and self._vault is not None and kind in _PROMOTE_KINDS:
            try:
                self._vault.write(kind, topic, content, source)
            except Exception as exc:  # noqa: BLE001 - vault is best-effort
                log.warning("vault promote failed: %s", exc)
        log.info("learned [%s] %s", kind, topic)

    def _record_canonical(self, kind: str, topic: str, content: str, source: str) -> None:
        if self._ledger is None:
            return
        idempotency_key = hashlib.sha256(
            f"{kind}\0{topic}\0{content}\0{source}".encode("utf-8")
        ).hexdigest()
        try:
            self._ledger.remember(
                kind=kind,
                stable_key=f"{kind}:{topic}",
                content=content,
                source=source or kind,
                idempotency_key=f"local:{idempotency_key}",
                targets=("obsidian",),
            )
            if self._shadow is not None:
                self._shadow.project_pending()
        except Exception as exc:  # noqa: BLE001 - durable projection is fail-open
            log.warning("canonical memory record failed: %s", exc)

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if self._ledger is not None:
            try:
                rows = self._ledger.recall_current(query, limit=limit)
            except Exception as exc:  # noqa: BLE001 - memory must not break a turn
                log.warning("canonical recall failed: %s", exc)
                return []
            if rows and self._bus is not None:
                try:
                    self._bus.publish(EventType.MEMORY_RETRIEVE, {"query": query, "hits": len(rows)})
                except Exception:  # noqa: BLE001
                    pass
            return rows
        try:
            try:
                rows = self._db.execute(
                    """SELECT k.kind, k.topic, k.content, k.source
                       FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid
                       WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self._db.execute(
                    "SELECT kind, topic, content, source FROM knowledge "
                    "WHERE topic LIKE ? OR content LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", limit),
                ).fetchall()
        except sqlite3.Error as exc:  # closed/locked DB etc. — recall is best-effort
            log.warning("recall failed: %s", exc)
            return []
        if rows and self._bus is not None:
            try:
                self._bus.publish(EventType.MEMORY_RETRIEVE, {"query": query, "hits": len(rows)})
            except Exception:  # noqa: BLE001
                pass
        return [dict(r) for r in rows]
    def already_known(self, topic: str) -> bool:
        return bool(self.recall(topic, limit=1))

    def recent(self, session: str = "", limit: int = 30) -> list[dict[str, Any]]:
        session = session or self._session
        rows = self._db.execute(
            "SELECT role, content FROM episodic WHERE session = ? ORDER BY id DESC LIMIT ?",
            (session, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_episodic(self, session: str = "", limit: int = 20) -> list[dict]:
        """Return recent episodic turns for a session, newest first."""
        try:
            s = session or self._session
            rows = self._db.execute(
                "SELECT role, content, ts FROM episodic WHERE session=? "
                "ORDER BY id DESC LIMIT ?",
                (s, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            log.warning("recent_episodic failed: %s", exc)
            return []

    def search_episodic(self, query: str, *, session: str = "",
                        limit: int = 10) -> list[dict]:
        """FTS search across episodic turns. Falls back to LIKE on syntax error."""
        try:
            sql = ("SELECT e.session, e.role, e.content, e.ts "
                   "FROM episodic e WHERE e.content LIKE ? ")
            params: list = [f"%{query}%"]
            if session:
                sql += "AND e.session=? "
                params.append(session)
            sql += "ORDER BY e.id DESC LIMIT ?"
            params.append(limit)
            rows = self._db.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            log.warning("search_episodic failed: %s", exc)
            return []
        return [dict(r) for r in rows]

    def delete_memory(self, topic: str) -> int:
        """Delete all knowledge entries matching the given topic. Returns count deleted."""
        try:
            rows = self._db.execute(
                "SELECT id FROM knowledge WHERE topic=?", (topic,)
            ).fetchall()
            for row in rows:
                self._db.execute("DELETE FROM knowledge_fts WHERE rowid=?", (row["id"],))
            cur = self._db.execute("DELETE FROM knowledge WHERE topic=?", (topic,))
            self._db.commit()
            return cur.rowcount
        except sqlite3.Error as exc:
            log.warning("delete_memory failed: %s", exc)
            return 0

    def count(self) -> dict[str, int]:
        """Return counts of knowledge entries by kind."""
        try:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) AS n FROM knowledge GROUP BY kind"
            ).fetchall()
            return {r["kind"]: r["n"] for r in rows}
        except sqlite3.Error as exc:
            log.warning("count failed: %s", exc)
            return {}

    def purge_old_episodic(self, max_age_days: float = 30) -> int:
        """Delete episodic turns older than max_age_days. Returns count deleted."""
        try:
            cutoff = self._clock() - max_age_days * 86_400
            cur = self._db.execute(
                "DELETE FROM episodic WHERE ts < ?", (cutoff,)
            )
            self._db.commit()
            return cur.rowcount
        except sqlite3.Error as exc:
            log.warning("purge_old_episodic failed: %s", exc)
            return 0

    def export_backup(self) -> dict:
        """Export all episodic turns and knowledge entries as JSON-serialisable dicts.
        Suitable for disaster recovery — does not require direct SQLite access."""
        try:
            knowledge = [dict(r) for r in self._db.execute(
                "SELECT id, ts, kind, topic, content, source, importance FROM knowledge ORDER BY id"
            ).fetchall()]
        except sqlite3.Error as exc:
            log.warning("export_backup knowledge failed: %s", exc)
            knowledge = []
        try:
            episodic = [dict(r) for r in self._db.execute(
                "SELECT id, ts, session, role, content FROM episodic ORDER BY id"
            ).fetchall()]
        except sqlite3.Error as exc:
            log.warning("export_backup episodic failed: %s", exc)
            episodic = []
        return {
            "knowledge": knowledge,
            "episodic": episodic,
            "knowledge_count": len(knowledge),
            "episodic_count": len(episodic),
        }

    def count_episodic(self, session_id: str) -> int:
        """Return the number of episodic turns stored for a session."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM episodic WHERE session=?", (session_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def delete_session_memory(self, session_id: str) -> int:
        """Delete all episodic turns for a session. Returns count deleted."""
        cur = self._db.execute("DELETE FROM episodic WHERE session=?", (session_id,))
        if cur.rowcount:
            self._db.commit()
        return cur.rowcount

    def wipe_knowledge(self, kind: str | None = None) -> int:
        """Delete all knowledge entries, optionally filtered to a kind. Returns count deleted."""
        if kind is not None:
            cur = self._db.execute("DELETE FROM knowledge WHERE kind=?", (kind,))
        else:
            cur = self._db.execute("DELETE FROM knowledge")
        if cur.rowcount:
            self._db.commit()
        return cur.rowcount

    def list_topics(self, kind: str | None = None) -> list[str]:
        """Return all knowledge topics, optionally filtered by kind."""
        if kind is not None:
            rows = self._db.execute(
                "SELECT topic FROM knowledge WHERE kind=? ORDER BY topic", (kind,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT topic FROM knowledge ORDER BY topic"
            ).fetchall()
        return [r["topic"] for r in rows]

    def most_important_facts(self, limit: int = 10) -> list[dict]:
        """Return the highest-importance knowledge entries, importance descending."""
        try:
            rows = self._db.execute(
                "SELECT id, ts, kind, topic, content, source, importance "
                "FROM knowledge ORDER BY importance DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("most_important_facts failed: %s", exc)
            return []

    def memory_stats(self) -> dict:
        """Return a summary of stored memory: counts, avg importance, oldest/newest timestamps."""
        try:
            k_row = self._db.execute(
                "SELECT COUNT(*) AS n, AVG(importance) AS avg_imp, "
                "MIN(ts) AS oldest, MAX(ts) AS newest FROM knowledge"
            ).fetchone()
            e_row = self._db.execute(
                "SELECT COUNT(*) AS n FROM episodic"
            ).fetchone()
            kind_rows = self._db.execute(
                "SELECT kind, COUNT(*) AS n FROM knowledge GROUP BY kind"
            ).fetchall()
            return {
                "knowledge_count": int(k_row["n"]) if k_row else 0,
                "episodic_count": int(e_row["n"]) if e_row else 0,
                "avg_importance": round(float(k_row["avg_imp"]), 4) if k_row and k_row["avg_imp"] is not None else 0.0,
                "oldest_ts": k_row["oldest"] if k_row else None,
                "newest_ts": k_row["newest"] if k_row else None,
                "by_kind": {r["kind"]: r["n"] for r in kind_rows},
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_stats failed: %s", exc)
            return {"knowledge_count": 0, "episodic_count": 0, "avg_importance": 0.0,
                    "oldest_ts": None, "newest_ts": None, "by_kind": {}}

    def close(self) -> None:
        self._db.close()

    # --- internals --------------------------------------------------------------

    def _log_turn(self, session: str, role: str, content: str) -> None:
        self._db.execute(
            "INSERT INTO episodic(ts, session, role, content) VALUES(?,?,?,?)",
            (self._clock(), session, role, content),
        )
        self._db.commit()

    def _insert_knowledge(self, kind: str, topic: str, content: str,
                          source: str, importance: float) -> None:
        cur = self._db.execute(
            "INSERT INTO knowledge(ts, kind, topic, content, source, importance) "
            "VALUES(?,?,?,?,?,?)",
            (self._clock(), kind, topic, content, source, importance),
        )
        self._db.execute(
            "INSERT INTO knowledge_fts(rowid, topic, content, source) VALUES(?,?,?,?)",
            (cur.lastrowid, topic, content, source),
        )
        self._db.commit()
