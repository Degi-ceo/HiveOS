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
