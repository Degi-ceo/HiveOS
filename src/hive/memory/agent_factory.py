"""
agent_factory.py — HiveOS Mnemosyne identity factory.

Usage:
    from hive.memory.agent_factory import mem_for, recall_channel
    mem = mem_for("hive-researcher", session_id="task-xyz")
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mnemosyne import Mnemosyne  # type: ignore[import]

BANK = "hive-main"
CHANNEL = "hive-main"

_IDENTITIES: dict[str, tuple[str, str]] = {
    "hive":            ("agent",  CHANNEL),
    "hive-researcher": ("agent",  CHANNEL),
    "hive-coder":      ("agent",  CHANNEL),
    "hive-ops":        ("agent",  CHANNEL),
    "hive-system":     ("system", CHANNEL),
    "kamil":           ("human",  CHANNEL),
}


def _db_path() -> Path:
    home = os.getenv("MNEMOSYNE_HOME", "")
    if home:
        return Path(home) / "hive.db"
    return Path(__file__).parent.parent.parent.parent / "data" / "mnemosyne" / "hive.db"


def mem_for(author_id: str, session_id: str | None = None) -> Mnemosyne:
    """Return a Mnemosyne instance tagged with the given HiveOS author identity."""
    if author_id not in _IDENTITIES:
        raise ValueError(f"Unknown HiveOS author_id: {author_id!r}. "
                         f"Valid: {list(_IDENTITIES)}")
    author_type, channel_id = _IDENTITIES[author_id]
    return Mnemosyne(
        session_id=session_id or f"{author_id}-default",
        db_path=_db_path(),
        bank=BANK,
        author_id=author_id,
        author_type=author_type,
        channel_id=channel_id,
    )


def recall_channel(query: str, top_k: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
    """Recall across all agents in the hive-main channel."""
    return mem_for("hive").recall(query, top_k=top_k, channel_id=CHANNEL, **kwargs)


def recall_by(query: str, author_id: str, top_k: int = 5, **kwargs: Any) -> list[dict[str, Any]]:
    """Recall filtered to a single author."""
    return mem_for("hive").recall(query, top_k=top_k, author_id=author_id, **kwargs)
