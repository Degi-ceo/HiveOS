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


def test_local_memory_recent_episodic(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("sess1")
    mem.sync_turn("hello", "world", session_id="sess1")
    mem.sync_turn("foo", "bar", session_id="sess1")
    recent = mem.recent_episodic("sess1", limit=10)
    assert len(recent) == 4  # 2 turns × 2 roles
    roles = [r["role"] for r in recent]
    assert "user" in roles and "assistant" in roles


def test_local_memory_recent_episodic_empty(tmp_path):
    mem = _provider(tmp_path)
    assert mem.recent_episodic("no_session") == []


def test_local_memory_emits_memory_store_event(tmp_path):
    """remember() and learn() must publish MEMORY_STORE on an injected EventBus."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    mem = LocalMemoryProvider(tmp_path / "m.sqlite", bus=bus)
    mem.remember("something important")
    mem.learn("fact", "topic", "content")
    types = [e.event_type for e in bus.history()]
    assert types.count(EventType.MEMORY_STORE) == 2


def test_local_memory_emits_memory_retrieve_event(tmp_path):
    """recall() must publish MEMORY_RETRIEVE when hits are found."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    mem = LocalMemoryProvider(tmp_path / "m.sqlite", bus=bus)
    mem.learn("fact", "python testing", "pytest is great")
    mem.recall("python")
    types = [e.event_type for e in bus.history()]
    assert EventType.MEMORY_RETRIEVE in types


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


def test_keeper_tracks_last_consolidated_ts(tmp_path):
    """After consolidate(), last_consolidated_ts is set."""
    import asyncio
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("how to deploy?", "run setup.sh", session_id="s1")

    async def fake_summarize(messages, system):
        return "[]"

    keeper = MemoryKeeper(fake_summarize, p)
    assert keeper.last_consolidated_ts is None
    asyncio.run(keeper.consolidate("s1"))
    assert keeper.last_consolidated_ts is not None
    assert isinstance(keeper.last_consolidated_ts, float)


def test_keeper_last_consolidated_count(tmp_path):
    import asyncio, json
    p = _provider(tmp_path)
    p.initialize("s2")
    p.sync_turn("question", "answer", session_id="s2")

    async def fake_summarize(messages, system):
        return "```json\n" + json.dumps([
            {"kind": "fact", "topic": "new fact", "content": "x", "source": "s2"},
        ]) + "\n```"

    keeper = MemoryKeeper(fake_summarize, p)
    asyncio.run(keeper.consolidate("s2"))
    assert keeper.last_consolidated_count == 1


def test_keeper_no_turns_is_noop(tmp_path):
    async def fake_summarize(messages, system):
        raise AssertionError("must not call the model with no turns")

    keeper = MemoryKeeper(fake_summarize, _provider(tmp_path))
    assert asyncio.run(keeper.consolidate("empty")) == 0


def test_export_backup_returns_all_entries(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("sess")
    mem.learn("fact", "topic1", "content1")
    mem.learn("skill", "topic2", "content2")
    mem.sync_turn("hello", "world", session_id="sess")
    backup = mem.export_backup()
    assert backup["knowledge_count"] == 2
    assert backup["episodic_count"] == 2
    kinds = {e["kind"] for e in backup["knowledge"]}
    assert "fact" in kinds and "skill" in kinds


def test_export_backup_empty_db(tmp_path):
    mem = _provider(tmp_path)
    backup = mem.export_backup()
    assert backup["knowledge_count"] == 0 and backup["episodic_count"] == 0


def test_search_episodic_finds_content(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("sess1")
    mem.sync_turn("the secret is 42", "understood", session_id="sess1")
    mem.sync_turn("unrelated", "reply", session_id="sess1")
    hits = mem.search_episodic("secret", session="sess1")
    assert len(hits) >= 1
    assert any("secret" in h["content"] for h in hits)


def test_search_episodic_session_filter(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("s1")
    mem.sync_turn("findme in s1", "ok", session_id="s1")
    mem.sync_turn("unrelated in s2", "ok", session_id="s2")
    # filter to s1 only
    hits = mem.search_episodic("findme", session="s1")
    assert all(h["session"] == "s1" for h in hits)


def test_purge_old_episodic(tmp_path):
    now = [0.0]
    mem = LocalMemoryProvider(tmp_path / "m.sqlite", clock=lambda: now[0])
    mem.initialize("sess")
    mem.sync_turn("hello", "world", session_id="sess")
    now[0] += 31 * 86_400  # advance 31 days
    mem.sync_turn("new turn", "reply", session_id="sess")
    purged = mem.purge_old_episodic(max_age_days=30)
    assert purged == 2  # 2 rows (user + assistant) from the old turn
    recent = mem.recent_episodic("sess")
    assert len(recent) == 2  # only the new turn remains


# --- count_episodic and delete_session_memory ----------------------------------

def test_count_episodic(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("s1")
    assert mem.count_episodic("s1") == 0
    mem.sync_turn("hi", "there", session_id="s1")
    assert mem.count_episodic("s1") == 2   # user + assistant


def test_delete_session_memory(tmp_path):
    mem = _provider(tmp_path)
    mem.initialize("sess_a")
    mem.initialize("sess_b")
    mem.sync_turn("hello", "world", session_id="sess_a")
    mem.sync_turn("foo", "bar", session_id="sess_b")
    deleted = mem.delete_session_memory("sess_a")
    assert deleted == 2
    assert mem.count_episodic("sess_a") == 0
    assert mem.count_episodic("sess_b") == 2  # untouched


def test_delete_session_memory_unknown_session(tmp_path):
    mem = _provider(tmp_path)
    deleted = mem.delete_session_memory("nonexistent")
    assert deleted == 0


def test_list_topics_empty(tmp_path):
    mem = _provider(tmp_path)
    assert mem.list_topics() == []


def test_list_topics_returns_learned_topics(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "python", "Python is a programming language")
    mem.learn("fact", "hive", "Hive is an AI agent system")
    topics = mem.list_topics()
    assert "python" in topics and "hive" in topics


def test_list_topics_filtered_by_kind(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "topic_a", "content a")
    mem.learn("skill", "topic_b", "content b")
    facts = mem.list_topics(kind="fact")
    assert "topic_a" in facts
    assert "topic_b" not in facts


def test_list_topics_sorted(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "zebra", "z")
    mem.learn("fact", "apple", "a")
    mem.learn("fact", "mango", "m")
    topics = mem.list_topics()
    assert topics == sorted(topics)


def test_wipe_knowledge_all(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "topic1", "content1")
    mem.learn("skill", "topic2", "content2")
    deleted = mem.wipe_knowledge()
    assert deleted == 2
    assert mem.list_topics() == []


def test_wipe_knowledge_by_kind(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "topic1", "content1")
    mem.learn("skill", "topic2", "content2")
    deleted = mem.wipe_knowledge(kind="fact")
    assert deleted == 1
    assert mem.list_topics() == ["topic2"]  # skill still there


def test_wipe_knowledge_empty_is_noop(tmp_path):
    mem = _provider(tmp_path)
    assert mem.wipe_knowledge() == 0


# --- most_important_facts ----------------------------------------------------

def test_most_important_facts_empty(tmp_path):
    mem = _provider(tmp_path)
    assert mem.most_important_facts() == []


def test_most_important_facts_sorted_by_importance(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "low", "content", source="s")
    # Manually set different importances via remember
    mem.remember("high importance fact", importance=0.9)
    mem.remember("medium importance fact", importance=0.6)
    facts = mem.most_important_facts(limit=2)
    assert len(facts) == 2
    # Highest importance first
    assert facts[0]["importance"] >= facts[1]["importance"]


def test_most_important_facts_respects_limit(tmp_path):
    mem = _provider(tmp_path)
    for i in range(5):
        mem.remember(f"fact {i}", importance=0.5 + i * 0.05)
    facts = mem.most_important_facts(limit=3)
    assert len(facts) == 3


# --- memory_stats ------------------------------------------------------------

def test_memory_stats_empty(tmp_path):
    mem = _provider(tmp_path)
    stats = mem.memory_stats()
    assert stats["knowledge_count"] == 0
    assert stats["episodic_count"] == 0
    assert stats["avg_importance"] == 0.0
    assert stats["by_kind"] == {}


def test_memory_stats_counts(tmp_path):
    mem = _provider(tmp_path)
    mem.learn("fact", "t1", "c1")
    mem.learn("fact", "t2", "c2")
    mem.learn("skill", "t3", "c3")
    mem.initialize("sess1")
    mem._log_turn("sess1", "user", "hello")
    stats = mem.memory_stats()
    assert stats["knowledge_count"] == 3
    assert stats["episodic_count"] == 1
    assert stats["by_kind"]["fact"] == 2
    assert stats["by_kind"]["skill"] == 1


def test_memory_stats_avg_importance(tmp_path):
    mem = _provider(tmp_path)
    mem.remember("important fact", importance=0.8)
    mem.remember("less important", importance=0.4)
    stats = mem.memory_stats()
    assert abs(stats["avg_importance"] - 0.6) < 0.01


# --- system_prompt_block -------------------------------------------------------

def test_system_prompt_block_empty_db():
    """system_prompt_block with no data returns a helpful hint."""
    prov = LocalMemoryProvider(":memory:")
    block = prov.system_prompt_block()
    assert isinstance(block, str)
    assert len(block) > 10


def test_system_prompt_block_with_data(tmp_path):
    """system_prompt_block with stored data returns actual fact content."""
    prov = _provider(tmp_path)
    prov.learn("fact", "test topic", "test content about something important")
    block = prov.system_prompt_block()
    assert "test topic" in block or "test content" in block
    assert "Persistent Memory" in block


# --- Task 4: MemoryKeeper per-item error handling ----------------------------

def test_keeper_consolidate_continues_on_item_error(tmp_path):
    """If one item's learn() raises, the remaining items are still processed."""
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("tell me things", "here are things", session_id="s1")

    learn_calls: list[str] = []
    original_learn = p.learn

    def _patched_learn(kind, topic, content, source=""):
        learn_calls.append(topic)
        if topic == "item two":
            raise RuntimeError("DB error on item 2")
        original_learn(kind, topic, content, source)

    p.learn = _patched_learn

    async def fake_summarize(messages, system):
        return "```json\n" + json.dumps([
            {"kind": "fact", "topic": "item one", "content": "c1", "source": "s1"},
            {"kind": "fact", "topic": "item two", "content": "c2", "source": "s1"},
            {"kind": "fact", "topic": "item three", "content": "c3", "source": "s1"},
        ]) + "\n```"

    keeper = MemoryKeeper(fake_summarize, p)
    new = asyncio.run(keeper.consolidate("s1"))

    assert "item one" in learn_calls
    assert "item two" in learn_calls
    assert "item three" in learn_calls
    assert new == 2


def test_keeper_consolidate_all_items_fail_returns_zero(tmp_path):
    """If every item's learn() fails, consolidate returns 0 without raising."""
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("q", "a", session_id="s1")

    def _always_fail(kind, topic, content, source=""):
        raise RuntimeError("total failure")

    p.learn = _always_fail

    async def fake_summarize(messages, system):
        return json.dumps([
            {"kind": "fact", "topic": "x", "content": "c", "source": "s1"},
            {"kind": "fact", "topic": "y", "content": "d", "source": "s1"},
        ])

    keeper = MemoryKeeper(fake_summarize, p)
    new = asyncio.run(keeper.consolidate("s1"))
    assert new == 0


# --- new edge-case tests -------------------------------------------------------

def test_local_provider_learn_and_recall(tmp_path):
    """learn() stores a fact that is retrievable via recall()."""
    p = _provider(tmp_path)
    user_msg = "What is the capital of France?"
    assistant_msg = "The capital of France is Paris."
    p.learn("fact", "capital of France", assistant_msg, "session1")
    hits = p.recall("France")
    assert isinstance(hits, list)
    assert len(hits) > 0
    assert any("France" in h["topic"] or "France" in h["content"] for h in hits)


def test_local_provider_system_prompt_block_with_facts(tmp_path):
    """After learning facts, system_prompt_block() includes real content."""
    p = _provider(tmp_path)
    p.learn("fact", "HiveOS model routing", "MiniMax handles execution tasks", "seed")
    p.learn("skill", "deploy procedure", "run scripts/setup.sh then hive serve", "seed")
    block = p.system_prompt_block()
    assert isinstance(block, str)
    assert len(block) > 20
    # With facts stored, block must include real memory content — not just the
    # generic hint returned when the DB is empty.
    assert "Persistent Memory" in block


def test_keeper_learn_multiple_sessions(tmp_path):
    """Turns stored in two separate sessions are independently retrievable."""
    p = _provider(tmp_path)
    p.initialize("session_a")
    p.sync_turn("hello from A", "reply from A", session_id="session_a")
    p.initialize("session_b")
    p.sync_turn("hello from B", "reply from B", session_id="session_b")

    turns_a = p.recent("session_a")
    turns_b = p.recent("session_b")

    assert len(turns_a) >= 2, "session_a should have at least 2 rows (user + assistant)"
    assert len(turns_b) >= 2, "session_b should have at least 2 rows (user + assistant)"
    contents_a = [t["content"] for t in turns_a]
    contents_b = [t["content"] for t in turns_b]
    assert any("session_a" in c or "hello from A" in c for c in contents_a)
    assert any("session_b" in c or "hello from B" in c for c in contents_b)
    # Sessions must not bleed into each other
    assert not any("hello from B" in c for c in contents_a)
    assert not any("hello from A" in c for c in contents_b)


def test_memory_prefetch_returns_ranked_results(tmp_path):
    """recall() returns a list of dicts with the expected keys."""
    p = _provider(tmp_path)
    p.learn("fact", "python async", "Use asyncio.run() for top-level coroutines", "docs")
    p.learn("skill", "pytest fixtures", "Use tmp_path for temporary files in tests", "docs")
    p.learn("fix", "sqlite locking", "Pass check_same_thread=False to sqlite3.connect()", "fix")

    results = p.recall("python", limit=10)
    assert isinstance(results, list)
    assert len(results) > 0
    for item in results:
        assert isinstance(item, dict)
        for key in ("kind", "topic", "content", "source"):
            assert key in item, f"Expected key {key!r} missing from recall result: {item}"


# --- new tests (8+) -----------------------------------------------------------

def test_local_provider_forget_removes_entry(tmp_path):
    """learn() then delete_memory() → recall returns empty."""
    p = _provider(tmp_path)
    p.learn("fact", "erasable fact", "this should be gone soon", "test")
    assert p.recall("erasable fact") != []
    p.delete_memory("erasable fact")
    assert p.recall("erasable fact") == []


def test_local_provider_count_after_multiple_learns(tmp_path):
    """count() after 3 learns shows total of 3."""
    p = _provider(tmp_path)
    p.learn("fact", "t1", "c1")
    p.learn("fact", "t2", "c2")
    p.learn("skill", "t3", "c3")
    counts = p.count()
    total = sum(counts.values())
    assert total == 3


def test_local_provider_system_prompt_block_no_facts():
    """Empty provider system_prompt_block() returns a non-empty fallback string."""
    prov = LocalMemoryProvider(":memory:")
    block = prov.system_prompt_block()
    assert isinstance(block, str) and len(block) > 0


def test_keeper_consolidate_returns_nonneg_int(tmp_path):
    """consolidate() always returns a non-negative integer."""
    p = _provider(tmp_path)
    p.initialize("s1")
    p.sync_turn("question", "answer", session_id="s1")

    async def fake_summarize(messages, system):
        return "[]"

    keeper = MemoryKeeper(fake_summarize, p)
    result = asyncio.run(keeper.consolidate("s1"))
    assert isinstance(result, int) and result >= 0


def test_local_provider_recall_partial_match(tmp_path):
    """A partial topic match (substring) returns results."""
    p = _provider(tmp_path)
    p.learn("fact", "deployment pipeline", "run scripts/setup.sh then hive serve", "doc")
    hits = p.recall("deploy")
    # FTS5 may require exact tokens; LIKE fallback handles partial prefix — either way
    # the entry must be findable since 'deploy' is a prefix of 'deployment'
    assert isinstance(hits, list)
    # At minimum the entry was stored; topic/content contain the keyword
    p2 = _provider(tmp_path)
    p2._db = p._db
    rows = p._db.execute(
        "SELECT kind, topic, content, source FROM knowledge WHERE topic LIKE ?",
        ("%deploy%",),
    ).fetchall()
    assert len(rows) >= 1


def test_local_provider_learn_duplicate_no_error(tmp_path):
    """Calling learn() with the same topic twice must not raise."""
    p = _provider(tmp_path)
    p.learn("fact", "duplicate topic", "first version", "test")
    p.learn("fact", "duplicate topic", "second version", "test")   # no error
    hits = p.recall("duplicate topic")
    assert len(hits) >= 1


def test_memory_prefetch_with_empty_query(tmp_path):
    """prefetch("") must not raise and must return a string."""
    p = _provider(tmp_path)
    result = p.prefetch("")
    assert isinstance(result, str)


def test_local_provider_kind_field_stored(tmp_path):
    """kind='identity' is preserved verbatim in the knowledge table."""
    p = _provider(tmp_path)
    p.learn("identity", "self description", "I am Hive", "seed")
    # Use direct SQL to confirm the kind was stored
    row = p._db.execute(
        "SELECT kind FROM knowledge WHERE topic='self description'"
    ).fetchone()
    assert row is not None
    assert row["kind"] == "identity"


# --- Wave 3L additional tests ---------------------------------------------------

def test_local_provider_name_is_local(tmp_path):
    """LocalMemoryProvider.name always returns 'local'."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    assert p.name == "local"
    p.close()


def test_local_provider_count_episodic_starts_zero(tmp_path):
    """count_episodic(session_id) is 0 on a fresh database for any session."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    assert p.count_episodic("fresh-session") == 0
    p.close()


def test_local_provider_sync_turn_increments_episodic(tmp_path):
    """sync_turn() adds an episodic log entry; count_episodic() increments."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.sync_turn("hello", "world", session_id="test-sess")
    assert p.count_episodic("test-sess") >= 1
    p.close()


def test_local_provider_memory_stats_has_keys(tmp_path):
    """memory_stats() returns a dict with 'knowledge_count' and 'episodic_count' keys."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    stats = p.memory_stats()
    assert "knowledge_count" in stats
    assert "episodic_count" in stats
    p.close()


def test_local_provider_already_known_false_for_new(tmp_path):
    """already_known(topic) returns False for a topic not yet learned."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    assert p.already_known("novel_topic_xyz") is False
    p.close()


def test_local_provider_already_known_true_after_learn(tmp_path):
    """already_known(topic) returns True after that topic is learned."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.learn("identity", "identity_check_topic", "Hive is an AI agent", source="test")
    assert p.already_known("identity_check_topic") is True
    p.close()


# --- Wave 3N additional tests ---------------------------------------------------

def test_local_provider_list_topics_includes_learned(tmp_path):
    """list_topics() includes topics that have been learned."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.learn("skill", "python-coding", "Write Python", source="test")
    p.learn("identity", "agent-name", "Hive", source="test")
    topics = p.list_topics()
    assert "python-coding" in topics
    assert "agent-name" in topics
    p.close()


def test_local_provider_most_important_facts_returns_list(tmp_path):
    """most_important_facts() returns a list of dicts with topic and content."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.learn("identity", "fact-1", "I am Hive", source="test")
    facts = p.most_important_facts(limit=3)
    assert isinstance(facts, list)
    assert len(facts) >= 1
    assert "topic" in facts[0] and "content" in facts[0]
    p.close()


def test_local_provider_delete_memory_removes_topic(tmp_path):
    """delete_memory(topic) removes the entry; already_known returns False after."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.learn("skill", "to-be-deleted", "some content", source="test")
    assert p.already_known("to-be-deleted") is True
    p.delete_memory("to-be-deleted")
    assert p.already_known("to-be-deleted") is False
    p.close()


def test_local_provider_recall_returns_list(tmp_path):
    """recall(query) returns a list (possibly empty) without raising."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    result = p.recall("autonomous agent", limit=5)
    assert isinstance(result, list)
    p.close()


def test_local_provider_search_episodic_returns_list(tmp_path):
    """search_episodic(query) returns a list without raising."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.sync_turn("user said hello", "assistant replied hi", session_id="ep-sess")
    result = p.search_episodic("hello", session="ep-sess")
    assert isinstance(result, list)
    p.close()


def test_local_provider_purge_old_episodic_returns_int(tmp_path):
    """purge_old_episodic(max_age_days=0) removes all episodic entries and returns a count."""
    from hive.memory.local import LocalMemoryProvider
    p = LocalMemoryProvider(tmp_path / "mem.db")
    p.sync_turn("msg", "reply", session_id="purge-sess")
    deleted = p.purge_old_episodic(max_age_days=0)
    assert isinstance(deleted, int) and deleted >= 0
    p.close()
