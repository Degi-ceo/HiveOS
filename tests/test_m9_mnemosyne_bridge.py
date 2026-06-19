"""M9-b — Mnemosyne host-LLM async bridge: dedicated event loop + thread.

Tests verify that:
  1. set_host_llm_backend installs a sync callable that the inner provider receives.
  2. The sync callable crosses from a worker thread back to the dedicated async loop
     without touching the main event loop.
  3. The bridge handles inner providers that lack set_host_llm_backend (safe no-op).
  4. runtime.py wires the bridge automatically when the provider is HiveMnemosyneProvider.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.memory.mnemosyne_provider import HiveMnemosyneProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_inner(has_backend: bool = True):
    inner = MagicMock()
    if not has_backend:
        del inner.set_host_llm_backend  # remove so hasattr returns False
    return inner


def _make_adapter(response: str = "consolidated") -> MagicMock:
    adapter = MagicMock()
    from hive.llm.adapters.base import CompletionResult
    adapter.complete = AsyncMock(return_value=CompletionResult(text=response, model="fake"))
    return adapter


# ---------------------------------------------------------------------------
# Unit tests: set_host_llm_backend
# ---------------------------------------------------------------------------

def test_backend_wires_sync_callable_to_inner():
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("result-text")

    provider.set_host_llm_backend(adapter, model="test-model", api_key="k")

    inner.set_host_llm_backend.assert_called_once()
    sync_fn = inner.set_host_llm_backend.call_args[0][0]
    assert callable(sync_fn)


def test_sync_callable_returns_adapter_text():
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("hello from llm")

    provider.set_host_llm_backend(adapter, model="test-model", api_key="k")

    sync_fn = inner.set_host_llm_backend.call_args[0][0]
    result = sync_fn("summarise this memory")
    assert result == "hello from llm"


def test_sync_callable_works_from_worker_thread():
    """The sync callable must be callable from a non-asyncio thread."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("from thread")

    provider.set_host_llm_backend(adapter, model="m", api_key="k")
    sync_fn = inner.set_host_llm_backend.call_args[0][0]

    result_holder: list[str] = []
    exc_holder: list[Exception] = []

    def _worker():
        try:
            result_holder.append(sync_fn("prompt from thread"))
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert not exc_holder, f"thread raised: {exc_holder[0]}"
    assert result_holder == ["from thread"]


def test_sync_callable_does_not_use_running_event_loop():
    """Calling the bridge from inside an asyncio task must not raise 'attached to different loop'."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter("async-safe")

    provider.set_host_llm_backend(adapter, model="m", api_key="k")
    sync_fn = inner.set_host_llm_backend.call_args[0][0]

    result_holder: list[str] = []

    async def _task():
        # run_coroutine_threadsafe submits to the DEDICATED loop, not the running one
        result_holder.append(await asyncio.get_event_loop().run_in_executor(
            None, sync_fn, "cross-loop prompt"))

    asyncio.run(_task())
    assert result_holder == ["async-safe"]


def test_no_op_when_inner_lacks_backend():
    """Provider without set_host_llm_backend should not raise."""
    inner = _make_inner(has_backend=False)
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter()

    provider.set_host_llm_backend(adapter, model="m", api_key="k")  # must not raise


# ---------------------------------------------------------------------------
# Integration: runtime.py wires the bridge automatically
# ---------------------------------------------------------------------------

def test_runtime_wires_bridge_for_mnemosyne_provider(tmp_path, monkeypatch):
    """If build_mnemosyne_provider returns a HiveMnemosyneProvider, build() must wire it."""
    from hive.core.config import HiveConfig
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS

    inner = _make_inner()
    fake_provider = HiveMnemosyneProvider(inner)

    monkeypatch.setattr(
        "hive.runtime.build_mnemosyne_provider",
        lambda **_: fake_provider,
    )

    class _ScriptRouter:
        async def complete(self, *a, **kw):
            return CompletionResult(text="ok", model="fake")
        async def aclose(self): pass

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    HiveOS.build(cfg, router=_ScriptRouter())

    inner.set_host_llm_backend.assert_called_once()


# ---------------------------------------------------------------------------
# Additional unit tests for HiveMnemosyneProvider / LocalMemoryProvider
# ---------------------------------------------------------------------------

def test_hive_mnemosyne_provider_instantiation_with_mock_inner():
    """HiveMnemosyneProvider can be constructed with any inner object (duck-typed)."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    assert provider is not None
    assert provider._inner is inner


def test_learn_stores_fact_via_inner_handle_tool_call():
    """learn() must delegate to inner.handle_tool_call with hive_remember."""
    inner = _make_inner()
    inner.handle_tool_call.return_value = "stored: abcd1234"
    provider = HiveMnemosyneProvider(inner)
    provider.learn("fact", "the sky is blue", "The sky appears blue due to Rayleigh scattering")
    inner.handle_tool_call.assert_called_once()
    call_args = inner.handle_tool_call.call_args
    assert call_args[0][0] == "hive_remember"
    payload = call_args[0][1]
    assert "the sky is blue" in payload["content"]


def test_recall_delegates_to_inner_recall():
    """recall() must call inner.recall when the method is available."""
    inner = _make_inner()
    inner.recall.return_value = [{"score": 0.9, "content": "blue sky fact"}]
    provider = HiveMnemosyneProvider(inner)
    results = provider.recall("sky", limit=3)
    inner.recall.assert_called_once_with("sky", top_k=3)
    assert results == [{"score": 0.9, "content": "blue sky fact"}]


def test_system_prompt_block_returns_inner_value():
    """system_prompt_block() must return what the inner returns when non-empty."""
    inner = _make_inner()
    inner.system_prompt_block.return_value = "## Persistent Memory\n- fact: sky is blue"
    provider = HiveMnemosyneProvider(inner)
    block = provider.system_prompt_block()
    assert "Persistent Memory" in block
    assert len(block) > 0


def test_prefetch_returns_inner_context():
    """prefetch() must return the memory context string from the inner."""
    inner = _make_inner()
    inner.prefetch.return_value = "<memory-context>\n- [0.85] sky fact\n</memory-context>"
    provider = HiveMnemosyneProvider(inner)
    result = provider.prefetch("sky", session_id="s1")
    assert "memory-context" in result
    inner.prefetch.assert_called_once_with("sky", session_id="s1")


def test_local_memory_provider_learn_and_recall():
    """LocalMemoryProvider.learn() stores a fact and recall() finds it."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "sky color", "The sky is blue.", "test")
    hits = mem.recall("sky color")
    assert len(hits) >= 1
    assert any("blue" in h["content"] for h in hits)


def test_local_memory_provider_delete_removes_entry():
    """delete_memory() must remove the matching knowledge entry."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "deletable-topic", "This should be removed.", "test")
    assert mem.already_known("deletable-topic")
    deleted = mem.delete_memory("deletable-topic")
    assert deleted >= 1
    assert not mem.already_known("deletable-topic")


def test_local_memory_provider_system_prompt_block_with_facts():
    """system_prompt_block() must return a non-empty string when facts exist."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "important-fact", "This is stored knowledge.", "test")
    block = mem.system_prompt_block()
    assert isinstance(block, str)
    assert len(block) > 0


def test_local_memory_provider_prefetch_returns_list_like():
    """prefetch() must return a string (not raise) — callers treat empty string as no context."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("skill", "topic-x", "Some skill content.", "test")
    result = mem.prefetch("topic-x")
    assert isinstance(result, str)


# --- Six additional bridge / provider tests -------------------------------------------

def test_hive_mnemosyne_provider_system_prompt_block_fail_open():
    """If inner.system_prompt_block() raises, the provider returns '' and does not crash."""
    inner = _make_inner()
    inner.system_prompt_block.side_effect = RuntimeError("Mnemosyne offline")
    provider = HiveMnemosyneProvider(inner)
    result = provider.system_prompt_block()
    assert result == ""


def test_hive_mnemosyne_provider_prefetch_fail_open():
    """If inner.prefetch() raises, provider returns '' and does not crash."""
    inner = _make_inner()
    inner.prefetch.side_effect = OSError("disk error")
    provider = HiveMnemosyneProvider(inner)
    result = provider.prefetch("anything")
    assert result == ""


def test_hive_mnemosyne_provider_handle_tool_call_returns_string():
    """handle_tool_call() always returns a str — it coerces the inner return value."""
    inner = _make_inner()
    inner.handle_tool_call.return_value = 42  # numeric return (unexpected but safe)
    provider = HiveMnemosyneProvider(inner)
    result = provider.handle_tool_call("hive_remember", {"content": "test"})
    assert isinstance(result, str)
    assert result == "42"


def test_hive_mnemosyne_provider_get_tool_schemas_delegates():
    """get_tool_schemas() must delegate to inner and return a list."""
    inner = _make_inner()
    inner.get_tool_schemas.return_value = [
        {"name": "hive_remember", "description": "store", "parameters": {}}
    ]
    provider = HiveMnemosyneProvider(inner)
    schemas = provider.get_tool_schemas()
    inner.get_tool_schemas.assert_called_once()
    assert isinstance(schemas, list)
    assert len(schemas) == 1
    assert schemas[0]["name"] == "hive_remember"


def test_hive_mnemosyne_provider_on_session_end_no_raise():
    """on_session_end() must not raise even when inner raises."""
    inner = _make_inner()
    inner.on_session_end.side_effect = Exception("end-of-session error")
    provider = HiveMnemosyneProvider(inner)
    provider.on_session_end()  # must not propagate the exception


def test_local_memory_provider_count_returns_dict():
    """count() returns a dict with kind -> integer entries after learning a fact."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "counted-topic", "Count this entry.", "test")
    counts = mem.count()
    assert isinstance(counts, dict)
    assert counts.get("fact", 0) >= 1


def test_local_memory_provider_list_topics_contains_learned_topic():
    """list_topics() must include the topic that was just learned."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "unique-topic-xyz", "Unique content for testing.", "test")
    topics = mem.list_topics()
    assert isinstance(topics, list)
    assert "unique-topic-xyz" in topics


# --- Six additional tests (batch 3) -------------------------------------------

def test_hive_mnemosyne_provider_sync_turn_delegates_to_inner():
    """sync_turn() must forward user/assistant content to inner.sync_turn."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    provider.sync_turn("hello user", "hello assistant", session_id="s1")
    inner.sync_turn.assert_called_once_with(
        "hello user", "hello assistant", session_id="s1"
    )


def test_hive_mnemosyne_provider_sync_turn_fail_open():
    """sync_turn() must not raise even when inner.sync_turn raises."""
    inner = _make_inner()
    inner.sync_turn.side_effect = RuntimeError("db locked")
    provider = HiveMnemosyneProvider(inner)
    provider.sync_turn("u", "a")  # must not propagate


def test_hive_mnemosyne_provider_initialize_delegates_to_inner():
    """initialize() must call inner.initialize with the correct session_id."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    provider.initialize("my-session", hermes_home="/tmp/hive")
    inner.initialize.assert_called_once_with("my-session", hermes_home="/tmp/hive")


def test_hive_mnemosyne_provider_initialize_fail_open():
    """initialize() must swallow exceptions from the inner and not crash."""
    inner = _make_inner()
    inner.initialize.side_effect = RuntimeError("Mnemosyne unavailable")
    provider = HiveMnemosyneProvider(inner)
    provider.initialize("session-x")  # must not raise


def test_hive_mnemosyne_provider_close_calls_inner_close():
    """close() must call inner.close() when the method is present."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    provider.close()
    inner.close.assert_called_once()


def test_local_memory_provider_purge_old_episodic_removes_old_turns():
    """purge_old_episodic() must delete turns older than max_age_days and return count."""
    import time
    from hive.memory.local import LocalMemoryProvider

    past_ts = time.time() - 40 * 86_400  # 40 days ago

    mem = LocalMemoryProvider(":memory:")
    mem._db.execute(
        "INSERT INTO episodic(ts, session, role, content) VALUES(?,?,?,?)",
        (past_ts, "old-session", "user", "ancient message"),
    )
    mem._db.commit()

    deleted = mem.purge_old_episodic(max_age_days=30)
    assert deleted >= 1
    # The old turn must be gone
    rows = mem._db.execute(
        "SELECT * FROM episodic WHERE session='old-session'"
    ).fetchall()
    assert len(rows) == 0


# --- Wave 5 additional tests (6) -----------------------------------------------

def test_hive_mnemosyne_provider_recall_delegates_to_inner():
    """recall() must forward the query and top_k=limit to inner.recall."""
    inner = _make_inner()
    inner.recall.return_value = [{"content": "some memory", "score": 0.9}]
    provider = HiveMnemosyneProvider(inner)
    results = provider.recall("test query", limit=3)
    inner.recall.assert_called_once_with("test query", top_k=3)
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["content"] == "some memory"


def test_hive_mnemosyne_provider_recall_fail_open():
    """recall() must return [] when inner.recall raises."""
    inner = _make_inner()
    inner.recall.side_effect = RuntimeError("recall error")
    provider = HiveMnemosyneProvider(inner)
    results = provider.recall("anything")
    assert results == []


def test_hive_mnemosyne_provider_already_known_true():
    """already_known() returns True when inner.recall returns at least one result."""
    inner = _make_inner()
    inner.recall.return_value = [{"content": "fact", "score": 0.8}]
    provider = HiveMnemosyneProvider(inner)
    assert provider.already_known("fact topic") is True


def test_hive_mnemosyne_provider_already_known_false():
    """already_known() returns False when inner.recall returns empty list."""
    inner = _make_inner()
    inner.recall.return_value = []
    provider = HiveMnemosyneProvider(inner)
    assert provider.already_known("unknown topic") is False


def test_hive_mnemosyne_provider_learn_calls_handle_tool_call():
    """learn() must invoke inner.handle_tool_call with hive_remember and the formatted payload."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    provider.learn("fact", "my-topic", "important content", "test-source")
    inner.handle_tool_call.assert_called_once()
    call_args = inner.handle_tool_call.call_args
    assert call_args[0][0] == "hive_remember"
    payload = call_args[0][1]["content"]
    assert "my-topic" in payload
    assert "important content" in payload


def test_hive_mnemosyne_provider_handle_tool_call_fail_open():
    """handle_tool_call() must return an error string instead of raising."""
    inner = _make_inner()
    inner.handle_tool_call.side_effect = Exception("tool crash")
    provider = HiveMnemosyneProvider(inner)
    result = provider.handle_tool_call("hive_remember", {"content": "x"})
    assert isinstance(result, str)
    assert len(result) > 0


# --- Wave 3Q additional tests ---------------------------------------------------

def test_hive_mnemosyne_provider_name_attribute():
    """HiveMnemosyneProvider has a name attribute identifying the memory backend."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    assert hasattr(provider, "name")
    assert isinstance(provider.name, str)


def test_hive_mnemosyne_provider_inner_accessible():
    """The inner object is accessible via _inner attribute."""
    inner = _make_inner()
    provider = HiveMnemosyneProvider(inner)
    assert provider._inner is inner


def test_local_memory_provider_already_known_false_for_new_topic():
    """already_known() returns False for a topic that was never learned."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    assert mem.already_known("never-learned-topic-xyz") is False
    mem.close()


def test_local_memory_provider_already_known_true_after_learn():
    """already_known() returns True after the topic is learned."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "known-topic", "content", "test")
    assert mem.already_known("known-topic") is True
    mem.close()


def test_hive_mnemosyne_provider_set_backend_no_raise_without_inner_support():
    """set_host_llm_backend() must not raise even when inner lacks the method."""
    inner = MagicMock(spec=[])  # no attributes at all
    provider = HiveMnemosyneProvider(inner)
    adapter = _make_adapter()
    provider.set_host_llm_backend(adapter, model="m", api_key="k")  # must not raise


def test_local_memory_provider_recall_empty_when_no_match():
    """recall() returns an empty list when no matching facts exist."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    results = mem.recall("completely-unique-query-xyz-123")
    assert isinstance(results, list)
    mem.close()


# --- Wave 3W-B additional tests (mnemosyne_bridge) ----------------------------

def test_wave3w_local_memory_provider_remember_is_recalled():
    """remember() stores a raw entry that recall() can find again."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.remember("The capital of France is Paris", topic="france-capital")
    hits = mem.recall("france capital")
    assert any("Paris" in h["content"] for h in hits)
    mem.close()


def test_wave3w_local_memory_provider_recent_returns_logged_turns():
    """recent() returns episodic turns for a given session in chronological order."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.initialize("sess-abc")
    mem.sync_turn("hello", "world", session_id="sess-abc")
    turns = mem.recent("sess-abc")
    assert len(turns) >= 2
    roles = [t["role"] for t in turns]
    assert "user" in roles and "assistant" in roles
    mem.close()


def test_wave3w_local_memory_provider_recent_episodic_returns_newest_first():
    """recent_episodic() returns rows ordered newest first (reverse-chronological)."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.initialize("sess-epi")
    mem.sync_turn("first turn user", "first turn assistant", session_id="sess-epi")
    mem.sync_turn("second turn user", "second turn assistant", session_id="sess-epi")
    rows = mem.recent_episodic("sess-epi")
    assert len(rows) >= 2
    # newest first means the last-inserted row comes first
    assert "second" in rows[0]["content"]
    mem.close()


def test_wave3w_local_memory_provider_search_episodic_matches_content():
    """search_episodic() must return turns whose content matches the query."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.initialize("sess-search")
    mem.sync_turn("unique-search-term-xyz user", "reply", session_id="sess-search")
    results = mem.search_episodic("unique-search-term-xyz", session="sess-search")
    assert len(results) >= 1
    assert any("unique-search-term-xyz" in r["content"] for r in results)
    mem.close()


def test_wave3w_local_memory_provider_export_backup_includes_knowledge():
    """export_backup() must include at least one knowledge entry after learn()."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "backup-topic", "content for backup test", "test")
    backup = mem.export_backup()
    assert "knowledge" in backup and "episodic" in backup
    assert backup["knowledge_count"] >= 1
    assert any(k["topic"] == "backup-topic" for k in backup["knowledge"])
    mem.close()


def test_wave3w_local_memory_provider_memory_stats_after_learn():
    """memory_stats() must report at least 1 knowledge entry after learn()."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("fact", "stats-topic", "content", "test")
    stats = mem.memory_stats()
    assert stats["knowledge_count"] >= 1
    assert isinstance(stats["avg_importance"], float)
    mem.close()


def test_wave3w_local_memory_provider_wipe_knowledge_by_kind():
    """wipe_knowledge(kind=...) removes only entries with that kind."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.learn("skill", "skill-topic", "a skill", "test")
    mem.learn("fact", "fact-topic", "a fact", "test")
    deleted = mem.wipe_knowledge(kind="skill")
    assert deleted >= 1
    remaining = mem.recall("skill-topic")
    assert all(h["kind"] != "skill" for h in remaining)
    # fact entry must still be present
    assert mem.already_known("fact-topic")
    mem.close()


def test_wave3w_local_memory_provider_count_episodic_increments():
    """count_episodic() increments by 2 per sync_turn (user + assistant)."""
    from hive.memory.local import LocalMemoryProvider
    mem = LocalMemoryProvider(":memory:")
    mem.initialize("sess-count")
    before = mem.count_episodic("sess-count")
    mem.sync_turn("question", "answer", session_id="sess-count")
    after = mem.count_episodic("sess-count")
    assert after == before + 2
    mem.close()


def test_wave3w_hive_mnemosyne_provider_close_no_raise_when_inner_has_no_close():
    """close() must not raise when the inner object has neither close nor shutdown."""
    from unittest.mock import MagicMock
    inner = MagicMock(spec=[])  # no attributes at all
    provider = HiveMnemosyneProvider(inner)
    provider.close()  # must not raise
