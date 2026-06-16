"""P4 — context: session store (persist + FTS), compaction, prefix-cache prompt build."""
from __future__ import annotations

import asyncio

from hive.core.soul import SOUL
from hive.core.types import Message, Role
from hive.context.compaction import compact
from hive.context.prompt_builder import (
    build_messages, restore_or_build_system_prompt, system_prompt,
)
from hive.context.session_store import SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.sqlite")


# --- session store -------------------------------------------------------------

def test_persist_and_ordered_recall(tmp_path):
    s = _store(tmp_path)
    s.append("s1", Role.USER, "deploy the gateway on port 8088")
    s.append("s1", Role.ASSISTANT, "running uvicorn now")
    msgs = s.messages("s1")
    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT]
    assert msgs[0].content.startswith("deploy")


def test_fts_search_scoped(tmp_path):
    s = _store(tmp_path)
    s.append("s1", Role.USER, "the MiniMax endpoint is anthropic-compatible")
    s.append("s2", Role.USER, "unrelated chatter about lunch")
    hits = s.search("MiniMax")
    assert len(hits) == 1 and hits[0]["session"] == "s1"
    assert s.search("MiniMax", session_id="s2") == []


def test_messages_limit_returns_tail_in_order(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.append("s1", Role.USER, f"m{i}")
    tail = s.messages("s1", limit=2)
    assert [m.content for m in tail] == ["m3", "m4"]


def test_summary_slot(tmp_path):
    s = _store(tmp_path)
    s.update_summary("s1", "did the thing")
    assert s.get_summary("s1") == "did the thing"


# --- list_sessions / delete_session (items 42-43) -----------------------------

def test_session_store_list_sessions(tmp_path):
    s = _store(tmp_path)
    s.append("alpha", Role.USER, "hello")
    s.append("beta", Role.USER, "world")
    sessions = s.list_sessions()
    assert "alpha" in sessions
    assert "beta" in sessions


def test_session_store_delete_session(tmp_path):
    s = _store(tmp_path)
    s.append("to_delete", Role.USER, "msg1")
    s.append("to_delete", Role.ASSISTANT, "msg2")
    deleted = s.delete_session("to_delete")
    assert deleted == 2
    assert s.messages("to_delete") == []
    assert "to_delete" not in s.list_sessions()


def test_session_store_count_messages(tmp_path):
    s = _store(tmp_path)
    assert s.count_messages("empty") == 0
    s.append("sess", Role.USER, "hello")
    s.append("sess", Role.ASSISTANT, "world")
    s.append("sess", Role.USER, "again")
    assert s.count_messages("sess") == 3
    # Deleting the session resets the count
    s.delete_session("sess")
    assert s.count_messages("sess") == 0


# --- prompt builder (prefix cache) --------------------------------------------

def test_system_prompt_includes_soul_and_memory():
    sp = system_prompt("## mem guidance")
    assert sp.startswith(SOUL) and "mem guidance" in sp


def test_prefix_cache_byte_exact_restore(tmp_path):
    s = _store(tmp_path)
    first = restore_or_build_system_prompt(s, "s1", memory_block="MEM A")
    # later turn with DIFFERENT input must still return byte-identical prompt
    second = restore_or_build_system_prompt(s, "s1", memory_block="MEM TOTALLY DIFFERENT")
    assert first == second
    assert "MEM A" in first and SOUL in first


def test_build_messages_puts_recall_as_user_not_system():
    history = [Message(role=Role.ASSISTANT, content="prior")]
    msgs = build_messages(history, "do X", recall_block="known fact")
    assert all(m.role is not Role.SYSTEM for m in msgs)        # prefix stays stable
    assert msgs[-1].content == "do X"
    assert any("known fact" in m.content for m in msgs[:-1])


# --- compaction ----------------------------------------------------------------

def _convo(n: int) -> list[Message]:
    return [Message(role=Role.USER if i % 2 == 0 else Role.ASSISTANT, content=f"turn{i}")
            for i in range(n)]


def test_compact_noop_under_trigger():
    msgs = _convo(5)
    out = asyncio.run(compact(msgs, summarizer=_unused, trigger=24))
    assert out == msgs


async def _unused(messages, system):  # pragma: no cover - must not be called
    raise AssertionError("summarizer should not be called")


def test_compact_protects_head_and_tail_and_summarizes_middle():
    msgs = _convo(30)

    async def summ(middle, system):
        assert system.startswith("Summarize")
        return "MIDDLE SUMMARY"

    out = asyncio.run(compact(msgs, summarizer=summ, head=2, tail=6, trigger=24))
    assert out[:2] == msgs[:2]                 # head preserved (prefix cache)
    assert out[-6:] == msgs[-6:]               # tail preserved (continuity)
    assert out[2].role is Role.USER and "MIDDLE SUMMARY" in out[2].content
    assert len(out) == 2 + 1 + 6


def test_compact_deterministic_fallback_on_failure():
    msgs = _convo(30)

    async def boom(middle, system):
        raise RuntimeError("model down")

    out = asyncio.run(compact(msgs, summarizer=boom, head=2, tail=6, trigger=24))
    assert out == [*msgs[:2], *msgs[-6:]]      # middle dropped, never raised


# ---------------------------------------------------------------------------
# context/title.py — generate_title()
# ---------------------------------------------------------------------------

def test_title_basic():
    from hive.context.title import generate_title

    async def _summarize(msgs, system):
        return "Memory system review"

    title = asyncio.run(generate_title("how does the memory system work?", _summarize))
    assert title == "Memory system review"


def test_title_strips_quotes():
    from hive.context.title import generate_title

    async def _summarize(msgs, system):
        return '"Clean Title"'

    title = asyncio.run(generate_title("test", _summarize))
    assert title == "Clean Title"


def test_title_fallback_on_error():
    from hive.context.title import generate_title

    async def _summarize(msgs, system):
        raise RuntimeError("model down")

    title = asyncio.run(generate_title("test", _summarize))
    assert title == "Untitled"


def test_title_truncates_at_80():
    from hive.context.title import generate_title

    async def _summarize(msgs, system):
        return "X" * 100

    title = asyncio.run(generate_title("test", _summarize))
    assert len(title) <= 80


def test_title_empty_response_returns_untitled():
    from hive.context.title import generate_title

    async def _summarize(msgs, system):
        return ""

    title = asyncio.run(generate_title("test", _summarize))
    assert title == "Untitled"


def test_session_store_stats(tmp_path):
    s = _store(tmp_path)
    s.append("s1", Role.USER, "hello")
    s.append("s1", Role.ASSISTANT, "hi")
    s.append("s2", Role.USER, "hey")
    stats = s.stats()
    assert stats["sessions"] == 2
    assert stats["messages"] == 3
    assert "active" in stats["by_status"]


def test_session_store_stats_empty(tmp_path):
    s = _store(tmp_path)
    stats = s.stats()
    assert stats["sessions"] == 0 and stats["messages"] == 0


def test_session_store_delete_archived(tmp_path):
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("old_sess")
    # Manually force status to archived and old timestamp
    s._db.execute("UPDATE sessions SET status='archived', updated=0 WHERE id='old_sess'")
    s._db.commit()
    # 100 days later
    now[0] = 100 * 86_400
    deleted = s.delete_archived(max_age_days=90)
    assert deleted == 1
    assert "old_sess" not in s.list_sessions()


def test_session_store_delete_archived_keeps_recent(tmp_path):
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("recent_sess")
    s._db.execute("UPDATE sessions SET status='archived' WHERE id='recent_sess'")
    s._db.commit()
    # Only 10 days later — within the 90-day max_age
    now[0] = 10 * 86_400
    deleted = s.delete_archived(max_age_days=90)
    assert deleted == 0
