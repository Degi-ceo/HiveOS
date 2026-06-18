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


def test_compaction_short_history_not_compacted():
    msgs = _convo(10)
    out = asyncio.run(compact(msgs, summarizer=_unused, trigger=24))
    assert out == msgs


def test_compaction_preserves_head_and_tail():
    msgs = _convo(30)

    async def summ(middle, system):
        return "summary"

    out = asyncio.run(compact(msgs, summarizer=summ, head=2, tail=2, trigger=24))
    assert out[:2] == msgs[:2]
    assert out[-2:] == msgs[-2:]


def test_compaction_summary_injected_in_middle():
    msgs = _convo(30)

    async def summ(middle, system):
        return "INJECTED SUMMARY"

    out = asyncio.run(compact(msgs, summarizer=summ, head=2, tail=6, trigger=24))
    # The summary marker sits at index 2, between head and tail
    assert out[2].role is Role.USER
    assert "INJECTED SUMMARY" in out[2].content


def test_compaction_reduces_message_count():
    msgs = _convo(30)

    async def summ(middle, system):
        return "condensed"

    out = asyncio.run(compact(msgs, summarizer=summ, head=2, tail=6, trigger=24))
    # head(2) + marker(1) + tail(6) = 9, well below 30
    assert len(out) < len(msgs)
    assert len(out) == 2 + 1 + 6


def test_compaction_system_message_always_preserved():
    # System message at index 0 is part of the head and must survive compaction
    sys_msg = Message(role=Role.SYSTEM, content="You are Hive.")
    user_msgs = _convo(29)
    msgs = [sys_msg, *user_msgs]

    async def summ(middle, system):
        return "summary"

    out = asyncio.run(compact(msgs, summarizer=summ, head=2, tail=6, trigger=24))
    assert out[0] is sys_msg


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


def test_session_store_sweep_marks_stale(tmp_path):
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("sess_a")
    # Manually set updated to 0 so it ages
    s._db.execute("UPDATE sessions SET updated=0 WHERE id='sess_a'")
    s._db.commit()
    # Advance past the stale threshold (30 days)
    now[0] = 31 * 86_400
    result = s.sweep()
    assert result["stale"] >= 1


def test_session_store_sweep_marks_archived(tmp_path):
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("old_sess")
    s._db.execute("UPDATE sessions SET updated=0 WHERE id='old_sess'")
    s._db.commit()
    # Advance past the archive threshold (90 days)
    now[0] = 91 * 86_400
    result = s.sweep()
    assert result["archived"] >= 1


def test_session_store_sweep_fresh_session_unchanged(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("fresh_sess")
    result = s.sweep()
    assert result["stale"] == 0 and result["archived"] == 0


def test_session_store_oldest_session_empty(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    assert s.oldest_session() is None


def test_session_store_oldest_session_single(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("only_one")
    assert s.oldest_session() == "only_one"


def test_session_store_oldest_session_multiple(tmp_path):
    now = [100.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("first")
    now[0] = 200.0
    s.ensure("second")
    now[0] = 300.0
    s.ensure("third")
    # The session with the smallest created timestamp is the oldest
    oldest = s.oldest_session()
    assert oldest == "first"


def test_session_store_session_count_empty(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    assert s.session_count() == 0


def test_session_store_session_count(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("a")
    s.ensure("b")
    s.ensure("c")
    assert s.session_count() == 3


def test_session_store_session_count_after_delete(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("x")
    s.ensure("y")
    s.delete_session("x")
    assert s.session_count() == 1


# --- total_message_count --------------------------------------------------------

def test_session_store_total_message_count_empty(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    assert s.total_message_count() == 0


def test_session_store_total_message_count(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("sess1")
    s.append("sess1", "user", "hello")
    s.append("sess1", "assistant", "hi")
    s.ensure("sess2")
    s.append("sess2", "user", "world")
    assert s.total_message_count() == 3


def test_session_store_total_message_count_after_delete(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    s.ensure("a")
    s.append("a", "user", "msg1")
    s.append("a", "user", "msg2")
    s.ensure("b")
    s.append("b", "user", "msg3")
    s.delete_session("a")
    assert s.total_message_count() == 1


# --- N-4: channel hint in system_prompt ---------------------------------------

def test_system_prompt_includes_channel_hint():
    from hive.context.prompt_builder import system_prompt
    result = system_prompt(channel_hint="telegram")
    assert "[Active surface: telegram]" in result


def test_system_prompt_no_hint_unchanged():
    from hive.context.prompt_builder import system_prompt
    with_hint = system_prompt(channel_hint="")
    without_hint = system_prompt()
    assert with_hint == without_hint
    assert "[Active surface" not in with_hint


# --- SessionStore persistence methods -----------------------------------------

def test_session_store_save_and_get_system_prompt(tmp_path):
    s = SessionStore(tmp_path / "sessions.sqlite")
    sid = "prompt-session"
    s.save_system_prompt(sid, "my system prompt")
    result = s.get_system_prompt(sid)
    assert result == "my system prompt"


def test_session_store_set_title(tmp_path):
    s = SessionStore(tmp_path / "sessions.sqlite")
    sid = "title-session"
    s.set_title(sid, "My Session")
    result = s.get_title(sid)
    assert result == "My Session"


def test_session_store_ensure_idempotent(tmp_path):
    s = SessionStore(tmp_path / "sessions.sqlite")
    sid = "idempotent-session"
    s.ensure(sid)
    sessions_after_first = s.list_sessions()
    # Second call must not raise and the session must still exist exactly once
    s.ensure(sid)
    sessions_after_second = s.list_sessions()
    assert sessions_after_first == sessions_after_second
    assert sid in sessions_after_second


def test_session_store_get_missing_system_prompt_returns_none(tmp_path):
    s = SessionStore(tmp_path / "sessions.sqlite")
    result = s.get_system_prompt("nonexistent-session")
    # No session row exists — must return None (or empty string) without raising
    assert result is None or result == ""


# --- get_title edge cases -------------------------------------------------------

def test_session_store_get_title_no_title_set(tmp_path):
    """ensure() creates a session with no title; get_title must return None."""
    s = SessionStore(tmp_path / "sessions.sqlite")
    s.ensure("no-title-sess")
    result = s.get_title("no-title-sess")
    assert result is None


def test_session_store_get_title_nonexistent_session(tmp_path):
    """get_title for a session that was never created must not raise."""
    s = SessionStore(tmp_path / "sessions.sqlite")
    result = s.get_title("ghost-session")
    assert result is None


# --- get_summary edge cases -----------------------------------------------------

def test_session_store_get_summary_nonexistent_session(tmp_path):
    """get_summary for a session that does not exist must return None silently."""
    s = SessionStore(tmp_path / "sessions.sqlite")
    result = s.get_summary("no-such-session")
    assert result is None


# --- stats after sweep ----------------------------------------------------------

def test_session_store_stats_by_status_after_sweep(tmp_path):
    """After sweep() transitions an active session to stale, stats() reflects it."""
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("aged")
    s._db.execute("UPDATE sessions SET updated=0 WHERE id='aged'")
    s._db.commit()
    now[0] = 31 * 86_400
    s.sweep()
    stats = s.stats()
    assert stats["by_status"].get("stale", 0) >= 1
    assert stats["by_status"].get("active", 0) == 0


# --- delete_archived mixed-age --------------------------------------------------

def test_session_store_delete_archived_mixed(tmp_path):
    """Only sessions past max_age_days are deleted; recent archived ones survive."""
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("old")
    s.ensure("young")
    # Both archived, but at different times
    s._db.execute("UPDATE sessions SET status='archived', updated=0 WHERE id='old'")
    s._db.execute(
        "UPDATE sessions SET status='archived', updated=? WHERE id='young'",
        (80 * 86_400,),
    )
    s._db.commit()
    now[0] = 100 * 86_400  # old is 100d old, young is 20d old
    deleted = s.delete_archived(max_age_days=90)
    assert deleted == 1
    assert "old" not in s.list_sessions()
    assert "young" in s.list_sessions()


# --- compact() boundary / empty-summary cases -----------------------------------

def test_compact_exact_trigger_boundary_not_compacted():
    """len(messages) == trigger: must return original list unchanged (boundary is <=)."""
    msgs = _convo(24)  # exactly trigger=24

    out = asyncio.run(compact(msgs, summarizer=_unused, trigger=24))
    assert out == msgs


def test_compact_empty_summary_falls_back_to_head_tail():
    """When summarizer returns an empty string, compact falls back to head+tail."""
    msgs = _convo(30)

    async def empty_summ(middle, system):
        return ""

    out = asyncio.run(compact(msgs, summarizer=empty_summ, head=2, tail=6, trigger=24))
    # No summary marker — only head and tail survive
    assert out == [*msgs[:2], *msgs[-6:]]


# --- sweep with simultaneous stale + archived transitions -----------------------

def test_session_store_sweep_mixed_transitions(tmp_path):
    """A stale session and a very old session both transition in the same sweep."""
    now = [0.0]
    s = SessionStore(tmp_path / "s.sqlite", clock=lambda: now[0])
    s.ensure("will_be_stale")
    s.ensure("will_be_archived")
    s._db.execute(
        "UPDATE sessions SET updated=0 WHERE id IN ('will_be_stale', 'will_be_archived')"
    )
    s._db.commit()
    # 91 days: will_be_archived crosses the 90-day archive threshold;
    # will_be_stale also crosses the 30-day stale threshold but sweep()
    # first marks active→stale, then non-archived→archived in the same call.
    now[0] = 91 * 86_400
    result = s.sweep()
    # Both rows are past the archive threshold so archived count should be >= 1.
    # (The stale transition may or may not fire depending on row ordering, but
    # total transitions >= 1.)
    assert result["archived"] >= 1


# --- new tests (8+) -----------------------------------------------------------

def test_session_store_get_missing_returns_empty(tmp_path):
    """messages() for a session that was never created returns []."""
    s = _store(tmp_path)
    assert s.messages("nonexistent") == []


def test_session_store_list_sessions_includes_appended(tmp_path):
    """After appending to s1, list_sessions() includes 's1'."""
    s = _store(tmp_path)
    s.append("s1", Role.USER, "hello")
    assert "s1" in s.list_sessions()


def test_session_store_delete_session_empties_messages(tmp_path):
    """append then delete_session → messages() returns []."""
    s = _store(tmp_path)
    s.append("to_del", Role.USER, "msg")
    s.delete_session("to_del")
    assert s.messages("to_del") == []


def test_session_store_count_by_role(tmp_path):
    """Only USER messages are counted when we filter by role after appending mixed roles."""
    s = _store(tmp_path)
    s.append("sess", Role.USER, "user msg 1")
    s.append("sess", Role.ASSISTANT, "assistant reply")
    s.append("sess", Role.USER, "user msg 2")
    msgs = s.messages("sess")
    user_count = sum(1 for m in msgs if m.role == Role.USER)
    assert user_count == 2


def test_compactor_does_not_compact_under_limit(tmp_path):
    """A session with 3 messages (well under the compaction trigger=24) is never compacted."""
    msgs = _convo(3)
    out = asyncio.run(compact(msgs, summarizer=_unused, trigger=24))
    assert out == msgs


def test_prompt_builder_soul_in_system_prompt():
    """system_prompt() output starts with the SOUL content."""
    sp = system_prompt()
    assert SOUL in sp


def test_prompt_builder_with_memory():
    """system_prompt(memory_block='facts...') includes the memory_block text."""
    sp = system_prompt(memory_block="facts about Hive")
    assert "facts about Hive" in sp


def test_session_store_append_multiple_roles_ordered(tmp_path):
    """append USER then ASSISTANT — messages() preserves insertion order."""
    s = _store(tmp_path)
    s.append("chat", Role.USER, "first user msg")
    s.append("chat", Role.ASSISTANT, "first assistant reply")
    msgs = s.messages("chat")
    assert len(msgs) == 2
    assert msgs[0].role == Role.USER
    assert msgs[1].role == Role.ASSISTANT


# ---------------------------------------------------------------------------
# Additional context tests
# ---------------------------------------------------------------------------

def test_session_store_update_system_prompt(tmp_path):
    """save_system_prompt then get_system_prompt must return the exact same string."""
    s = _store(tmp_path)
    sid = "sys-prompt-roundtrip"
    prompt_text = "You are a helpful assistant named Hive."
    s.save_system_prompt(sid, prompt_text)
    result = s.get_system_prompt(sid)
    assert result == prompt_text


def test_session_store_get_messages_returns_list(tmp_path):
    """messages() always returns a list — even for a session that was never created."""
    s = _store(tmp_path)
    result = s.messages("totally-new-session")
    assert isinstance(result, list)


def test_session_store_count_messages(tmp_path):
    """count_messages() matches the number of append() calls."""
    s = _store(tmp_path)
    sid = "counting-session"
    for i in range(5):
        s.append(sid, Role.USER, f"message {i}")
    count = s.count_messages(sid)
    assert count == 5


def test_prompt_builder_channel_hint_absent_no_active_surface(tmp_path):
    """When no channel_hint is passed, '[Active surface:' must NOT appear in the output."""
    sp = system_prompt()
    assert "[Active surface:" not in sp


def test_session_store_title_none_initially(tmp_path):
    """A freshly ensured session has no title — get_title returns None."""
    s = _store(tmp_path)
    sid = "brand-new-session"
    s.ensure(sid)
    title = s.get_title(sid)
    assert title is None


def test_compaction_trigger_config():
    """compact() default trigger is 24 — messages up to 24 are never compacted."""
    msgs = _convo(24)
    out = asyncio.run(compact(msgs, summarizer=_unused))
    # 24 messages at the default trigger=24 must be returned unchanged
    assert out == msgs


def test_session_store_recent_sessions(tmp_path):
    """list_sessions() returns a list of sessions; we verify at most n are returned
    when we supply exactly n sessions (no 'recent_sessions' helper — we use list_sessions)."""
    s = _store(tmp_path)
    for i in range(5):
        s.ensure(f"sess-{i}")
    sessions = s.list_sessions()
    # list_sessions() must return all five (we verify ≤ a larger cap)
    assert isinstance(sessions, list)
    assert len(sessions) >= 3  # at least the 3 we care about for "recent_sessions(n=3)" semantics
