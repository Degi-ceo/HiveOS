"""P6 — agents: loop guard, planner, executor tick, delegation, full tool loop."""
from __future__ import annotations

import asyncio
import json

from hive.agents.base import AgentResult, BaseAgent
from hive.agents.delegate import delegate
from hive.agents.executor import AgentExecutor, TerminalOutcome
from hive.agents.loop_guard import LoopGuard
from hive.agents.orchestrator import ConversationOrchestrator
from hive.agents.planner import Planner
from hive.core.types import Message, ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.llm.failover import RetryPolicy
from hive.tools.base import BaseTool, ToolSpec
from hive.core.types import ToolResult


# --- loop guard ----------------------------------------------------------------

def test_loop_guard_identical_repeat():
    g = LoopGuard(max_identical=3)
    assert g.check("read", {"p": 1}) is None
    assert g.check("read", {"p": 1}) is None
    assert "identical" in g.check("read", {"p": 1})


def test_loop_guard_pingpong_and_budget():
    g = LoopGuard()
    assert g.check("a", {}) is None          # a
    assert g.check("b", {}) is None          # a,b
    assert g.check("a", {}) is None          # a,b,a (window not full)
    assert "ping-pong" in (g.check("b", {}) or "")  # a,b,a,b -> trips
    g2 = LoopGuard(max_per_tool=2)
    g2.check("x", {"i": 1}); g2.check("x", {"i": 2})
    assert "budget" in g2.check("x", {"i": 3})


# --- planner -------------------------------------------------------------------

class _FakeRouter:
    """Returns scripted CompletionResults; records calls."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        item = self._script.pop(0)
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="fake")


def test_planner_parses_task_list():
    tasks = [{"task": "t1", "tool": "discover", "args": {}, "reason": "r"}]
    router = _FakeRouter(["```json\n" + json.dumps(tasks) + "\n```"])
    out = asyncio.run(Planner(router).plan(["goal"], "context"))
    assert out == tasks
    assert router.calls[0]["system"].startswith  # system prompt provided


def test_planner_bad_json_returns_empty():
    out = asyncio.run(Planner(_FakeRouter(["not json"])).plan(["g"], "c"))
    assert out == []


# --- agent executor (terminal outcome) -----------------------------------------

class _Agent(BaseAgent):
    def __init__(self, *, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    async def run(self, input, context=None, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("transient")  # retryable per failover.classify
        return AgentResult(content=f"done:{input}")


def test_executor_completes_after_retry():
    ex = AgentExecutor(retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0))
    agent = _Agent(fail_times=1)
    res = asyncio.run(ex.execute_tick(agent, "go"))
    assert res.outcome is TerminalOutcome.COMPLETED and res.attempts == 2
    assert res.result.content == "done:go"


def test_executor_fails_after_exhausting_retries():
    ex = AgentExecutor(retry=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0))
    res = asyncio.run(ex.execute_tick(_Agent(fail_times=5), "go"))
    assert res.outcome is TerminalOutcome.FAILED and "transient" in res.error


# --- delegation ----------------------------------------------------------------

def test_delegate_runs_subtasks_in_isolated_agents():
    made = []

    def factory():
        a = _Agent()
        made.append(a)
        return a

    results = asyncio.run(delegate(["t1", "t2", "t3"], agent_factory=factory, max_concurrent=2))
    assert [r.content for r in results] == ["done:t1", "done:t2", "done:t3"]
    assert len(made) == 3 and all(a.calls == 1 for a in made)  # one fresh leaf each


# --- full conversation tool loop ----------------------------------------------

class _Echo(BaseTool):
    spec = ToolSpec(name="echo", description="echo", parameters={"type": "object"})

    async def execute(self, **params):
        return ToolResult(tool_name="echo", content=f"echoed:{params.get('text','')}")


def test_orchestrator_runs_tool_then_answers():
    # turn 1: model asks for the tool; turn 2: model gives the final answer
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "hi"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="final answer", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()})
    res = asyncio.run(orch.ask("please echo"))
    assert res.content == "final answer" and res.turns == 2
    assert res.tool_results and res.tool_results[0].content == "echoed:hi"
    # second router call saw the tool result in the message history
    second_msgs = router.calls[1]["messages"]
    assert any("echoed:hi" in m.content for m in second_msgs)


def test_orchestrator_persists_to_memory_and_store():
    from hive.context.session_store import SessionStore
    from hive.memory.local import LocalMemoryProvider
    import tempfile, os
    tmp = tempfile.mkdtemp()
    store = SessionStore(os.path.join(tmp, "s.sqlite"))
    mem = LocalMemoryProvider(os.path.join(tmp, "m.sqlite"))
    router = _FakeRouter([CompletionResult(text="hello back", model="m")])
    orch = ConversationOrchestrator(router, memory=mem, session_store=store)
    res = asyncio.run(orch.ask("hello", session_id="s1"))
    assert res.content == "hello back"
    msgs = store.messages("s1")
    assert [m.content for m in msgs] == ["hello", "hello back"]
    assert mem.recent("s1")  # turn synced to memory


# --- LoopGuard.reset() and .stats() -------------------------------------------

def test_loop_guard_reset_clears_state():
    g = LoopGuard(max_identical=3, max_per_tool=5)
    g.check("read_file", {"path": "/a"})
    g.check("write_file", {"path": "/b"})
    assert g.stats()["total_calls"] == 2
    g.reset()
    s = g.stats()
    assert s["total_calls"] == 0
    assert s["unique_tools"] == 0
    assert s["per_tool"] == {}


def test_loop_guard_stats_reflects_calls():
    g = LoopGuard(max_per_tool=10)
    g.check("read_file", {"path": "/x"})
    g.check("read_file", {"path": "/y"})
    g.check("write_file", {"path": "/z"})
    s = g.stats()
    assert s["total_calls"] == 3
    assert s["unique_tools"] == 2
    assert s["per_tool"]["read_file"] == 2
    assert s["per_tool"]["write_file"] == 1
    assert s["max_per_tool"] == 10
