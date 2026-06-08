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
