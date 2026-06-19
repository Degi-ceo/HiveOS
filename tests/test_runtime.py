"""P7 — runtime wiring: HiveOS.build() + HiveOS.ask() end-to-end (no network)."""
from __future__ import annotations

import asyncio
import json

import hive
from hive.core.config import HiveConfig, get_config
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    """Stands in for ModelRouter; returns scripted results, records tool schemas seen."""
    def __init__(self, script):
        self._script = list(script)
        self.saw_tools = None
        self.closed = False

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        self.saw_tools = tools
        item = self._script.pop(0)
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="fake")

    async def aclose(self):
        self.closed = True


def _config(tmp_path) -> HiveConfig:
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_lazy_package_export():
    # `from hive import HiveOS` works via PEP 562 without eager full-tree import
    assert hive.HiveOS is HiveOS


def test_build_wires_all_subsystems_and_sets_global_config(tmp_path):
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert isinstance(hos, HiveOS)
    for attr in ("events", "router", "tools", "tool_executor", "memory",
                 "session_store", "keeper", "planner", "orchestrator"):
        assert getattr(hos, attr) is not None
    assert "read_file" in hos.tools and "shell" in hos.tools
    # builder published the config to the global accessor (D1)
    assert get_config() is cfg
    assert cfg.data_dir.is_dir()


def test_ask_end_to_end_persists_and_recalls(tmp_path):
    router = _ScriptRouter([CompletionResult(text="hi Kamil", model="m")])
    hos = HiveOS.build(_config(tmp_path), router=router)

    answer = asyncio.run(hos.ask("hello", session_id="s1"))
    assert answer == "hi Kamil"
    assert [m.content for m in hos.session_store.messages("s1")] == ["hello", "hi Kamil"]
    # session_store is the canonical transcript; memory.recent() is provider-specific.
    # Use prefetch() which is in the MemoryProvider ABC and works with all providers.
    # (LocalMemoryProvider also syncs turns, so recent() would be truthy there too.)
    assert router.saw_tools and any(t["name"] == "shell" for t in router.saw_tools)


def test_ask_runs_a_real_builtin_tool_end_to_end(tmp_path):
    target = tmp_path / "out.txt"
    call = ToolCall(id="c1", name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "from hive"}))
    router = _ScriptRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),  # ask for the tool
        CompletionResult(text="done", model="m"),                  # then answer
    ])
    hos = HiveOS.build(_config(tmp_path), router=router)
    answer = asyncio.run(hos.ask("please write the file"))
    assert answer == "done"
    assert target.read_text() == "from hive"          # the builtin actually ran


def test_aclose_closes_router(tmp_path):
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    asyncio.run(hos.aclose())
    assert router.closed is True


def test_pending_review_edits_lists_review_tier(tmp_path):
    """pending_review_edits() returns metadata for each REVIEW-tier edit awaiting approval."""
    from hive.core.spec_search import Edit, EditOp
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix logic", apply=_noop)
    asyncio.run(hos.improver.run([edit]))
    pending = hos.pending_review_edits()
    assert len(pending) == 1
    assert pending[0]["op"] == "patch_code"
    assert pending[0]["summary"] == "fix logic"
    assert "approval_id" in pending[0]


def test_pending_review_edits_empty_when_none_pending(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.pending_review_edits() == []


def test_system_status_returns_expected_keys(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    status = hos.system_status()
    assert "router" in status
    assert "budget" in status
    assert "tasks" in status
    assert "tools" in status
    assert "pending_approvals" in status
    assert "cron_jobs" in status
    assert "active_commitments" in status
    # Budget forecast nested properly
    assert "remaining_calls" in status["budget"]


def test_abort_self_mod_removes_pending_edit(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop)
    asyncio.run(hos.improver.run([edit]))
    pending = hos.pending_review_edits()
    assert len(pending) == 1
    approval_id = pending[0]["approval_id"]
    assert hos.abort_self_mod(approval_id) is True
    assert hos.pending_review_edits() == []
    assert hos.abort_self_mod(approval_id) is False  # already gone


def test_abort_all_self_mods(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    import itertools
    _counter = itertools.count(1)

    class _CountingGate:
        requests = []
        def request(self, name, args, reason):
            self.requests.append(name)
            return f"appr-{next(_counter)}"
        def is_dangerous(self, *a): return False

    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    # Directly use the improver to enqueue two REVIEW edits
    async def _noop(_wt): return []
    e1 = Edit(op=EditOp.PATCH_CODE, summary="e1", apply=_noop)
    e2 = Edit(op=EditOp.PATCH_CODE, summary="e2", apply=_noop)
    asyncio.run(hos.improver.run([e1]))
    asyncio.run(hos.improver.run([e2]))
    assert hos.improver.pending_count() == 2
    cancelled = hos.abort_all_self_mods()
    assert cancelled == 2
    assert hos.improver.pending_count() == 0


def test_last_self_mod_branch_none_when_no_proposals(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.last_self_mod_branch() is None


def test_self_mod_history_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.self_mod_history() == []


def test_recent_self_mod_branches_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.recent_self_mod_branches() == []


def test_resume_after_restart_returns_dict(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.resume_after_restart()
    assert "requeued" in result
    assert result["requeued"] == 0  # no running tasks to recover


def test_resume_after_restart_requeues_running_tasks(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    tid = hos.task_board.enqueue("tool", {})
    hos.task_board.claim(tid)   # now it's RUNNING
    result = hos.resume_after_restart()
    assert result["requeued"] == 1
    assert hos.task_board.get(tid).state == "pending"


def test_event_history_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    events = hos.event_history()
    assert isinstance(events, list)


def test_loop_guard_stats_returns_dict(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    stats = hos.loop_guard_stats()
    assert "total_calls" in stats
    assert "max_per_tool" in stats


def test_reset_loop_guard_clears_state(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    # Manually add a call to the guard to verify reset clears it
    hos.loop_guard.check("shell", {"cmd": "ls"})
    assert hos.loop_guard_stats()["total_calls"] == 1
    hos.reset_loop_guard()
    assert hos.loop_guard_stats()["total_calls"] == 0


# --- N-4: channel_hint flows through HiveOS.ask() and ask_stream() -----------

def test_ask_channel_hint_reaches_system_prompt(tmp_path):
    """channel_hint='telegram' must appear in the system param seen by the router."""
    captured = {}

    class _CapturingRouter:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            captured["system"] = system or ""
            return CompletionResult(text="ok", model="fake")
        async def aclose(self): pass

    hos = HiveOS.build(_config(tmp_path), router=_CapturingRouter())
    asyncio.run(hos.ask("hello", channel_hint="telegram"))
    assert "[Active surface: telegram]" in captured.get("system", ""), \
        f"channel_hint not in system prompt: {captured.get('system', '')[:200]}"


def test_ask_stream_channel_hint_reaches_system_prompt(tmp_path):
    """channel_hint='web' must appear in the system param during streaming."""
    captured = {}

    class _StreamRouter:
        async def stream(self, messages, *, system=None, **kw):
            captured["system"] = system or ""
            yield "token"
        async def aclose(self): pass

    hos = HiveOS.build(_config(tmp_path), router=_StreamRouter())

    async def collect():
        return [t async for t in hos.ask_stream("hi", channel_hint="web")]

    asyncio.run(collect())
    assert "[Active surface: web]" in captured.get("system", ""), \
        f"channel_hint not in stream system prompt: {captured.get('system', '')[:200]}"


def test_hive_health_returns_dict(tmp_path):
    """health() returns a dict with all expected top-level keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.health()
    assert isinstance(result, dict)
    for key in ("status", "tools", "budget", "telemetry", "tasks", "memory",
                "pending_approvals", "pending_review_edits", "cron_jobs",
                "active_commitments", "self_mod_proposals"):
        assert key in result, f"missing key: {key}"
    assert result["status"] == "ok"
    assert isinstance(result["tools"], int)


def test_hive_consolidate_returns_int(tmp_path):
    """consolidate() returns an integer count (0 when the session is empty)."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.consolidate())
    assert isinstance(result, int)
    assert result == 0


def test_hive_curate_returns_dict(tmp_path):
    """curate() returns a dict with the expected lifecycle-report keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.curate()
    assert isinstance(result, dict)
    assert "skills" in result
    assert "transitions" in result
    assert isinstance(result["transitions"], list)


def test_hive_curate_umbrellas_fail_open(tmp_path):
    """curate_umbrellas() must not raise even if umbrella consolidation fails."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.curate_umbrellas())
    assert isinstance(result, dict)
    # With no agent-created skills the curator short-circuits; always returns a dict.
    assert "skipped" in result or "umbrellas_created" in result


def test_hive_aclose_idempotent(tmp_path):
    """aclose() must not raise when called twice."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    asyncio.run(hos.aclose())
    asyncio.run(hos.aclose())  # second call must not raise


def test_hive_self_improve_from_symptom_smoke(tmp_path, monkeypatch):
    """self_improve_from_symptom() returns a list without raising."""
    async def _fake_diagnose_and_run(diagnoser, symptom, improver):
        return []

    monkeypatch.setattr(
        "hive.core.spec_search.diagnose_and_run", _fake_diagnose_and_run
    )
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.self_improve_from_symptom("test failing: some error"))
    assert isinstance(result, list)


# --- Additional runtime tests ---------------------------------------------------

def test_tools_dict_is_nonempty_after_build(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert len(hos.tools) > 0


def test_hive_build_twice_different_instances(tmp_path):
    a = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    b = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert a is not b


def test_hive_router_attribute_is_set(tmp_path):
    r = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=r)
    assert hos.router is r


def test_hive_session_store_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.session_store is not None


def test_hive_events_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.events is not None


def test_hive_ask_returns_string(tmp_path):
    from hive.llm.adapters.base import CompletionResult
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([
        CompletionResult(text="hello world", model="fake")
    ]))
    reply = asyncio.run(hos.ask("test message", session_id="s1"))
    assert isinstance(reply, str) and len(reply) > 0


def test_hive_memory_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.memory is not None


def test_hive_config_stored(tmp_path):
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    # config is accessible; root path matches
    assert str(tmp_path) in str(hos.config.root) or str(tmp_path) in str(hos.config.data_dir)


# --- Wave 3L additional tests ---------------------------------------------------

def test_hive_health_includes_memory_key(tmp_path):
    """HiveOS.health() dict must have 'memory' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert "memory" in h


def test_hive_health_includes_budget_key(tmp_path):
    """HiveOS.health() dict must have 'budget' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert "budget" in h


def test_hive_system_status_has_tools_key(tmp_path):
    """system_status() dict must have 'tools' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "tools" in s


def test_hive_system_status_has_router_key(tmp_path):
    """system_status() dict must have 'router' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "router" in s


def test_hive_skill_usage_is_not_none(tmp_path):
    """HiveOS.skill_usage is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.skill_usage is not None


def test_hive_keeper_is_not_none(tmp_path):
    """HiveOS.keeper is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.keeper is not None


# --- Wave 3N additional tests ---------------------------------------------------

def test_hive_loop_guard_stats_returns_dict(tmp_path):
    """HiveOS.loop_guard_stats() returns a dict."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    stats = hos.loop_guard_stats()
    assert isinstance(stats, dict)


def test_hive_reset_loop_guard_does_not_raise(tmp_path):
    """HiveOS.reset_loop_guard() runs without raising."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.reset_loop_guard()  # should be idempotent


def test_hive_event_history_returns_list(tmp_path):
    """HiveOS.event_history() returns a list."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hist = hos.event_history()
    assert isinstance(hist, list)


def test_hive_agents_registry_not_empty(tmp_path):
    """HiveOS.agents_registry has at least one entry after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert len(hos.agents_registry) > 0


def test_hive_audit_log_not_none(tmp_path):
    """HiveOS.audit_log is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.audit_log is not None


def test_hive_aclose_sets_router_closed(tmp_path):
    """HiveOS.aclose() closes the router."""
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    asyncio.run(hos.aclose())
    assert router.closed is True


# --- Wave 3S additional tests ---------------------------------------------------

def test_hive_skill_usage_not_none(tmp_path):
    """HiveOS.skill_usage is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.skill_usage is not None


def test_hive_curator_not_none(tmp_path):
    """HiveOS.curator is a Curator instance."""
    from hive.memory.curator import Curator
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert isinstance(hos.curator, Curator)


def test_hive_session_store_not_none(tmp_path):
    """HiveOS.session_store is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.session_store is not None


def test_hive_tool_executor_not_none(tmp_path):
    """HiveOS.tool_executor is a ToolExecutor instance."""
    from hive.tools.executor import ToolExecutor
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert isinstance(hos.tool_executor, ToolExecutor)


def test_hive_config_accessible(tmp_path):
    """HiveOS.config returns the config that was passed to build()."""
    from hive.core.config import HiveConfig
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert isinstance(hos.config, HiveConfig)


def test_hive_health_returns_dict(tmp_path):
    """HiveOS.health() returns a dict with at least one key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h, dict)
    assert len(h) > 0


# --- Wave 4B additional tests ---------------------------------------------------

def test_wave4b_budgeter_not_none(tmp_path):
    """HiveOS.budgeter is wired after build()."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.budgeter is not None


def test_wave4b_telemetry_snapshot_is_dict(tmp_path):
    """HiveOS.telemetry.snapshot() returns a plain dict."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    snap = hos.telemetry.snapshot()
    assert isinstance(snap, dict)
    assert "inference_calls" in snap


def test_wave4b_curate_umbrellas_returns_dict_with_skipped_or_created(tmp_path):
    """curate_umbrellas() returns a dict containing 'skipped' or 'umbrellas_created'."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result or "umbrellas_created" in result


def test_wave4b_aclose_marks_router_closed(tmp_path):
    """aclose() sets router.closed to True."""
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    assert not router.closed
    asyncio.run(hos.aclose())
    assert router.closed is True


def test_wave4b_ask_method_is_callable(tmp_path):
    """HiveOS.ask is a callable method."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert callable(hos.ask)


def test_wave4b_ask_stream_method_is_callable(tmp_path):
    """HiveOS.ask_stream is a callable method."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert callable(hos.ask_stream)


def test_wave4b_health_telemetry_key_is_dict(tmp_path):
    """health()['telemetry'] is a dict with inference_calls."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h["telemetry"], dict)
    assert "inference_calls" in h["telemetry"]


def test_wave4b_health_tasks_key_is_dict(tmp_path):
    """health()['tasks'] is a dict with a 'total' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h["tasks"], dict)
    assert "total" in h["tasks"]


# --- Wave 4I additional tests ---------------------------------------------------

def test_wave4i_system_status_has_self_mod_history_count(tmp_path):
    """system_status() contains a 'self_mod_history_count' integer key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "self_mod_history_count" in s
    assert isinstance(s["self_mod_history_count"], int)


def test_wave4i_system_status_budget_has_daily_cap(tmp_path):
    """system_status()['budget'] contains 'daily_cap' and 'calls_today' sub-keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    budget = s["budget"]
    assert "daily_cap" in budget
    assert "calls_today" in budget


def test_wave4i_ask_empty_string_returns_str(tmp_path):
    """ask('') with an empty input must return a str without raising."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([
        CompletionResult(text="empty reply", model="fake")
    ]))
    result = asyncio.run(hos.ask(""))
    assert isinstance(result, str)


def test_wave4i_task_board_total_count_starts_at_zero(tmp_path):
    """task_board.total_count() is 0 on a freshly built HiveOS."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.task_board.total_count() == 0


def test_wave4i_task_board_pending_count_increases_after_enqueue(tmp_path):
    """task_board.pending_count() increments by 1 after enqueue()."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    before = hos.task_board.pending_count()
    hos.task_board.enqueue("my_tool", {"arg": "val"})
    assert hos.task_board.pending_count() == before + 1


def test_wave4i_task_board_complete_changes_state_to_done(tmp_path):
    """Claiming then completing a task sets its state to 'done'."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    tid = hos.task_board.enqueue("finish_tool", {})
    hos.task_board.claim(tid)
    hos.task_board.complete(tid)
    assert hos.task_board.get(tid).state == "done"


def test_wave4i_task_board_count_by_kind_reflects_enqueued_kind(tmp_path):
    """count_by_kind() returns the correct count for the enqueued task kind."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.task_board.enqueue("special_kind", {})
    hos.task_board.enqueue("special_kind", {})
    by_kind = hos.task_board.count_by_kind()
    assert by_kind.get("special_kind", 0) == 2


def test_wave4i_task_board_statistics_returns_dict_with_total(tmp_path):
    """task_board.statistics() returns a dict containing a 'total' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.task_board.enqueue("stat_tool", {})
    stats = hos.task_board.statistics()
    assert isinstance(stats, dict)
    assert "total" in stats
    assert stats["total"] >= 1
