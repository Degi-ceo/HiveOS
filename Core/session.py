"""
session.py — conversation context, backed by the Brain.

Builds the message list for a turn: SOUL system prompt + recent working memory
+ any recalled durable knowledge relevant to the user's message.
"""
from __future__ import annotations
from core import settings
from memory.brain import brain


def build(user_msg: str, bank: str = "default") -> tuple[str, list[dict]]:
    recalled = brain.recall(user_msg, limit=3)
    recall_note = ""
    if recalled:
        recall_note = "\n\n[Known from memory]\n" + "\n".join(
            f"- ({r['kind']}) {r['topic']}: {r['content'][:200]}" for r in recalled)
    system = settings.SOUL + recall_note
    msgs = [{"role": m["role"] if m["role"] in ("user", "assistant") else "user",
             "content": m["content"]} for m in brain.recent(bank, 20)]
    msgs.append({"role": "user", "content": user_msg})
    return system, msgs


def record(role: str, content: str, bank: str = "default") -> None:
    brain.remember(content, role=role, bank=bank)
