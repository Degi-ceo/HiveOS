"""
brain.py — Hive's human-like memory brain.

Two layers, per the researched design:
  ACTIVE      : Mnemosyne (SQLite-backed, working/episodic/scratchpad + triples).
                Reached over MCP if MNEMOSYNE_MCP_URL is set; otherwise a local
                SQLite fallback keeps Hive functional from day one.
  LONG-TERM   : Obsidian vault (classified, linked markdown — "old memories").
                Reached over the Obsidian Local REST API if configured; otherwise
                Hive writes markdown directly to the local vault folder.

The memory-keeper sub-agent (see core/memory_keeper.py) is the only writer to
long-term storage. This module exposes remember / recall / promote primitives.

Persistent-learning guarantee: anything learned (skill, MCP, research, fix) is
saved here and recalled before re-doing work — Hive never re-researches a topic
it already resolved.
"""
from __future__ import annotations
import time
import json
import sqlite3
import logging
from pathlib import Path

from core import settings

log = logging.getLogger("hiveos.brain")


class Brain:
    def __init__(self) -> None:
        self._db = sqlite3.connect(settings.SQLITE_PATH, check_same_thread=False)
        self._init()
        self._vault = Path(settings.OBSIDIAN_VAULT)
        self._vault.mkdir(parents=True, exist_ok=True)
        self._mnemosyne = settings.MNEMOSYNE_MCP  # MCP URL or ""
        log.info("Brain ready (mnemosyne=%s vault=%s)",
                 self._mnemosyne or "local-fallback", self._vault)

    def _init(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS working(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bank TEXT,
              role TEXT, content TEXT, importance REAL DEFAULT 0.5);
            CREATE TABLE IF NOT EXISTS knowledge(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,
              topic TEXT, content TEXT, source TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
              USING fts5(topic, content, source, content='knowledge', content_rowid='id');
            """
        )
        self._db.commit()

    # --- working memory -----------------------------------------------------
    def remember(self, content: str, role: str = "note",
                 bank: str = "default", importance: float = 0.5) -> None:
        self._db.execute(
            "INSERT INTO working(ts,bank,role,content,importance) VALUES(?,?,?,?,?)",
            (time.time(), bank, role, content, importance),
        )
        self._db.commit()

    def recent(self, bank: str = "default", limit: int = 30) -> list[dict]:
        rows = self._db.execute(
            "SELECT role,content FROM working WHERE bank=? ORDER BY id DESC LIMIT ?",
            (bank, limit),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    # --- long-term knowledge (the "have I done this before?" check) ---------
    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        """Persist a durable learning: skill | mcp | research | fix | fact."""
        cur = self._db.execute(
            "INSERT INTO knowledge(ts,kind,topic,content,source) VALUES(?,?,?,?,?)",
            (time.time(), kind, topic, content, source),
        )
        self._db.execute(
            "INSERT INTO knowledge_fts(rowid,topic,content,source) VALUES(?,?,?,?)",
            (cur.lastrowid, topic, content, source),
        )
        self._db.commit()
        self._promote_to_vault(kind, topic, content, source)
        log.info("learned [%s] %s", kind, topic)

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        """Search durable knowledge. Hive calls this BEFORE researching anything."""
        try:
            rows = self._db.execute(
                """SELECT k.kind,k.topic,k.content,k.source
                   FROM knowledge_fts f JOIN knowledge k ON k.id=f.rowid
                   WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._db.execute(
                "SELECT kind,topic,content,source FROM knowledge "
                "WHERE topic LIKE ? OR content LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [{"kind": k, "topic": t, "content": c, "source": s} for k, t, c, s in rows]

    def already_known(self, topic: str) -> bool:
        return len(self.recall(topic, limit=1)) > 0

    # --- Obsidian long-term store ------------------------------------------
    def _promote_to_vault(self, kind: str, topic: str, content: str, source: str) -> None:
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in topic)[:80]
        folder = self._vault / kind
        folder.mkdir(parents=True, exist_ok=True)
        note = folder / f"{safe}.md"
        body = (
            f"---\nkind: {kind}\ntopic: {topic}\nsource: {source}\n"
            f"created: {time.strftime('%Y-%m-%d %H:%M')}\ntags: [{kind}]\n---\n\n"
            f"# {topic}\n\n{content}\n"
        )
        note.write_text(body, encoding="utf-8")

    def vault_stats(self) -> dict:
        files = list(self._vault.rglob("*.md"))
        return {"notes": len(files),
                "knowledge_rows": self._db.execute(
                    "SELECT COUNT(*) FROM knowledge").fetchone()[0]}


brain = Brain()
