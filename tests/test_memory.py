"""P3 — memory layer: local provider (remember/recall), vault export, keeper."""
from __future__ import annotations

import asyncio
import json

from hive.memory.keeper import MemoryKeeper
from hive.memory.local import LocalMemoryProvider
from hive.memory.vault import ObsidianVault


def _provider(tmp_path, with_vault=False) -> LocalMemoryProvider:
    vault = ObsidianVault(tmp_path / "vault") if with_vault else None
    return LocalMemoryProvider(tmp_path / "mem.sqlite", vault=vault)


# --- local provider ------------------------------------------------------------

def test_remember_and_recall_roundtrip(tmp_path):
    p = _provider(tmp_path)
    p.remember("Kamil prefers Polish for chat, English for code")
    hits = p.recall("Polish")
    assert hits and "Polish" in hits[0]["content"]


def test_recall_empty_and_already_known(tmp_path):
    p = _provider(tmp_path)
    assert p.recall("nothing here") == []
    assert p.already_known("nothing here") is False
    p.learn("fix", "sqlite locking", "use check_same_thread=False", "session")
    assert p.already_known("sqlite") is True


def test_learn_promotes_to_vault(tmp_path):
    p = _provider(tmp_path, with_vault=True)
    p.learn("research", "MiniMax endpoint", "use the Anthropic-compatible path", "audit")
    note = tmp_path / "vault" / "research" / "MiniMax endpoint.md"
    assert note.is_file()
    assert "Anthropic-compatible" in note.read_text()
    # casual remember() does NOT create a vault note
    p.remember("ephemeral thought")
    assert ObsidianVault(tmp_path / "vault").stats()["notes"] == 1


def test_prefetch_block_and_failopen(tmp_path):
    p = _provider(tmp_path)
    assert p.prefetch("anything") == ""          # nothing stored yet
    p.learn("skill", "deploy", "run scripts/setup.sh", "doc")
    block = p.prefetch("deploy")
    assert block.startswith("## Recalled memory") and "deploy" in block
    p.close()                                     # force errors on the closed DB
    assert p.prefetch("deploy") == ""             # fail-open, no raise


def test_sync_turn_and_recent(tmp_path):
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("hello", "hi there", session_id="s1")
    recent = p.recent("s1")
    assert [r["role"] for r in recent] == ["user", "assistant"]
    assert recent[0]["content"] == "hello"


def test_tool_schemas_and_handle_tool_call(tmp_path):
    p = _provider(tmp_path)
    names = {s["function"]["name"] for s in p.get_tool_schemas()}
    assert names == {"remember", "recall"}
    assert p.handle_tool_call("remember", {"content": "x is y"}) == "Saved to memory."
    assert "x is y" in p.handle_tool_call("recall", {"query": "x"})
    assert p.handle_tool_call("recall", {"query": "zzz"}) == "No relevant memory found."
    assert "Unknown" in p.handle_tool_call("bogus", {})


# --- delete_memory / count (items 40-41) ---------------------------------------

def test_local_memory_delete(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "test topic", "test content")
    count = mem.delete_memory("test topic")
    assert count == 1
    hits = mem.recall("test topic")
    assert len(hits) == 0


def test_local_memory_count(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "t1", "content1")
    mem.learn("skill", "t2", "content2")
    mem.learn("fact", "t3", "content3")
    counts = mem.count()
    assert counts.get("fact", 0) == 2
    assert counts.get("skill", 0) == 1


# --- keeper --------------------------------------------------------------------

def test_keeper_consolidates_new_items_only(tmp_path):
    p = _provider(tmp_path, with_vault=True)
    p.initialize("s1")
    p.sync_turn("how do I deploy?", "run scripts/setup.sh then uvicorn", session_id="s1")
    p.learn("skill", "deploy", "already known", "seed")   # pre-existing -> must be skipped

    calls: list = []

    async def fake_summarize(messages, system):
        calls.append((messages, system))
        return "```json\n" + json.dumps([
            {"kind": "skill", "topic": "deploy", "content": "dup", "source": "s1"},
            {"kind": "fix", "topic": "uvicorn port", "content": "use 8088", "source": "s1"},
        ]) + "\n```"

    keeper = MemoryKeeper(fake_summarize, p)
    new = asyncio.run(keeper.consolidate("s1"))
    assert new == 1                               # only the unknown "uvicorn port"
    assert p.already_known("uvicorn") is True
    assert calls and calls[0][1].startswith("You are Hive's memory-keeper")


def test_keeper_no_turns_is_noop(tmp_path):
    async def fake_summarize(messages, system):
        raise AssertionError("must not call the model with no turns")

    keeper = MemoryKeeper(fake_summarize, _provider(tmp_path))
    assert asyncio.run(keeper.consolidate("empty")) == 0
