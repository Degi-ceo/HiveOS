"""
mnemosyne.py — Hive's memory system.

Three layers:
  - WORKING   : current session window (handled by SessionStore)
  - EPISODIC  : durable log of events/turns (SQLite)
  - SEMANTIC  : vector recall of facts/summaries (Qdrant)

Aux model writes summaries + extracts facts so the expensive model never
spends tokens on bookkeeping. Degrades gracefully if Qdrant is absent
(episodic-only mode).
"""
from __future__ import annotations
import os
import json
import time
import sqlite3
import logging
from pathlib import Path

from core.model_router import router, TaskKind

log = logging.getLogger("hiveos.memory")
DB = os.getenv("SQLITE_PATH", "./data/hiveos.db")
QDRANT_URL = os.getenv("QDRANT_URL", "")

Path(DB).parent.mkdir(parents=True, exist_ok=True)


class Mnemosyne:
    def __init__(self) -> None:
        self._db = sqlite3.connect(DB, check_same_thread=False)
        self._init_db()
        self._qdrant = self._try_qdrant()

    def _init_db(self) -> None:
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS episodic(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts REAL, session TEXT, role TEXT, content TEXT)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS facts(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts REAL, subject TEXT, fact TEXT)"""
        )
        self._db.commit()

    def _try_qdrant(self):
        if not QDRANT_URL:
            log.info("Qdrant disabled -> episodic-only memory")
            return None
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
            c = QdrantClient(url=QDRANT_URL)
            cols = [x.name for x in c.get_collections().collections]
            if "hiveos_semantic" not in cols:
                c.create_collection(
                    "hiveos_semantic",
                    vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
                )
            return c
        except Exception as e:  # noqa: BLE001
            log.warning("Qdrant unavailable: %s", e)
            return None

    # --- episodic -----------------------------------------------------------
    def log_turn(self, session: str, role: str, content: str) -> None:
        self._db.execute(
            "INSERT INTO episodic(ts,session,role,content) VALUES(?,?,?,?)",
            (time.time(), session, role, content),
        )
        self._db.commit()

    def recent(self, session: str, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT role,content FROM episodic WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    # --- semantic (fact extraction via aux model) ---------------------------
    async def consolidate(self, session: str) -> None:
        """Summarize recent turns + extract durable facts. Aux model only."""
        turns = self.recent(session, 30)
        if not turns:
            return
        convo = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        prompt = [
            {"role": "system", "content":
             "Extract durable facts about the user/projects as JSON list of "
             '{"subject","fact"}. Only stable facts. Return ONLY JSON.'},
            {"role": "user", "content": convo},
        ]
        try:
            raw = await router.complete(prompt, kind=TaskKind.AUX)
            facts = json.loads(raw.strip().strip("`").replace("json", "", 1))
            for f in facts:
                self._db.execute(
                    "INSERT INTO facts(ts,subject,fact) VALUES(?,?,?)",
                    (time.time(), f.get("subject", ""), f.get("fact", "")),
                )
            self._db.commit()
            log.info("Consolidated %d facts", len(facts))
        except Exception as e:  # noqa: BLE001
            log.warning("consolidate skipped: %s", e)

    def known_facts(self, limit: int = 50) -> list[str]:
        rows = self._db.execute(
            "SELECT subject,fact FROM facts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [f"{s}: {f}" for s, f in rows]


memory = Mnemosyne()
