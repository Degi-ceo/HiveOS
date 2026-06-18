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


# --- top_repeated_tools / call_count ----------------------------------------

def test_loop_guard_top_repeated_tools_empty():
    g = LoopGuard()
    assert g.top_repeated_tools() == []


def test_loop_guard_top_repeated_tools_sorted():
    g = LoopGuard(max_per_tool=100)
    for _ in range(3):
        g.check("read_file", {"path": "/x"})
    for _ in range(5):
        g.check("write_file", {"path": "/y"})
    g.check("search", {})
    top = g.top_repeated_tools(n=2)
    assert top[0] == ("write_file", 5)
    assert top[1] == ("read_file", 3)


def test_loop_guard_top_repeated_tools_respects_n():
    g = LoopGuard(max_per_tool=100)
    for tool in ["a", "b", "c", "d"]:
        g.check(tool, {})
    assert len(g.top_repeated_tools(n=2)) == 2


def test_loop_guard_call_count_zero_unknown_tool():
    g = LoopGuard()
    assert g.call_count("nonexistent") == 0


def test_loop_guard_call_count_tracks_tool():
    g = LoopGuard(max_per_tool=20)
    g.check("shell", {"cmd": "ls"})
    g.check("shell", {"cmd": "pwd"})
    assert g.call_count("shell") == 2


def test_loop_guard_call_count_resets_after_reset():
    g = LoopGuard(max_per_tool=20)
    g.check("shell", {})
    g.reset()
    assert g.call_count("shell") == 0


# --- N-3: TerminalOutcome on AgentResult --------------------------------------

def test_terminal_outcome_completed():
    from hive.agents.base import TerminalOutcome as TO
    router = _FakeRouter([CompletionResult(text="done", model="m")])
    orch = ConversationOrchestrator(router)
    res = asyncio.run(orch.ask("hello"))
    assert res.outcome == TO.COMPLETED


def test_terminal_outcome_max_turns():
    from hive.agents.base import TerminalOutcome as TO
    import json
    # Router always returns tool calls with unique args -> loop guard won't trip
    # max_per_tool set high enough to not interfere; max_iterations=3 is the limit
    calls = [ToolCall(id=f"c{i}", name="echo", arguments=json.dumps({"text": str(i)}))
             for i in range(20)]
    router = _FakeRouter([CompletionResult(text="", model="m", tool_calls=[c]) for c in calls])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()},
                                   max_iterations=3, max_per_tool=100)
    res = asyncio.run(orch.ask("loop"))
    assert res.outcome == TO.MAX_TURNS


def test_terminal_outcome_loop_guard():
    from hive.agents.base import TerminalOutcome as TO
    import json
    # Same tool call repeated past budget -> loop guard trips (max_per_tool=2)
    call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "same"}))
    router = _FakeRouter([CompletionResult(text="", model="m", tool_calls=[call])] * 10)
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()}, max_per_tool=2)
    res = asyncio.run(orch.ask("repeat"))
    assert res.outcome == TO.LOOP_GUARD


# --- Task 2: LoopGuard dedicated strategy coverage ----------------------------

def test_loop_guard_strategy1_exact_duplicate_detection():
    """Strategy 1: exact same tool+args repeated max_identical times trips the guard."""
    g = LoopGuard(max_identical=2)
    assert g.check("read_file", {"path": "/x"}) is None
    result = g.check("read_file", {"path": "/x"})
    assert result is not None
    assert "identical" in result
    assert "read_file" in result


def test_loop_guard_strategy1_different_args_do_not_trip():
    """Strategy 1: same tool with different args is not an identical repeat."""
    g = LoopGuard(max_identical=2)
    assert g.check("read_file", {"path": "/a"}) is None
    assert g.check("read_file", {"path": "/b"}) is None
    assert g.check("read_file", {"path": "/c"}) is None


def test_loop_guard_strategy2_pingpong_detected():
    """Strategy 2: A-B-A-B alternation is detected as ping-pong loop."""
    g = LoopGuard()
    g.check("tool_a", {"x": 1})
    g.check("tool_b", {"x": 2})
    g.check("tool_a", {"x": 1})
    result = g.check("tool_b", {"x": 2})
    assert result is not None
    assert "ping-pong" in result


def test_loop_guard_strategy2_pingpong_requires_full_window():
    """Strategy 2: A-B-A is only 3 calls — not enough to trip ping-pong."""
    g = LoopGuard()
    g.check("tool_a", {"x": 1})
    g.check("tool_b", {"x": 2})
    result = g.check("tool_a", {"x": 1})
    assert result is None


def test_loop_guard_strategy3_per_tool_budget_exceeded():
    """Strategy 3: same tool called more than max_per_tool times trips budget guard."""
    g = LoopGuard(max_per_tool=3)
    g.check("shell", {"cmd": "ls"})
    g.check("shell", {"cmd": "pwd"})
    g.check("shell", {"cmd": "whoami"})
    result = g.check("shell", {"cmd": "date"})
    assert result is not None
    assert "budget" in result
    assert "shell" in result


def test_loop_guard_strategy3_budget_per_tool_independent():
    """Strategy 3: budget is tracked per tool; different tools have independent budgets."""
    g = LoopGuard(max_per_tool=2)
    g.check("tool_a", {"n": 1})
    g.check("tool_a", {"n": 2})
    # tool_a is at budget but not yet over (budget trips on > max)
    assert g.check("tool_b", {"n": 1}) is None
    assert g.check("tool_b", {"n": 2}) is None
    # tool_a goes over
    result = g.check("tool_a", {"n": 3})
    assert result is not None and "tool_a" in result


def test_loop_guard_no_trip_on_diverse_calls():
    """No strategy fires when every call uses a different tool with unique args."""
    g = LoopGuard(max_identical=3, max_per_tool=5)
    tools = [("search", {"q": "a"}), ("read_file", {"p": "/x"}),
             ("write_file", {"p": "/y"}), ("shell", {"cmd": "ls"})]
    for name, args in tools:
        assert g.check(name, args) is None


# --- Task 1: AgentExecutor cancellation propagation ---------------------------

def test_executor_propagates_cancelled_error():
    """CancelledError from the agent must NOT be swallowed — it propagates immediately."""
    import pytest

    class _CancelAgent(BaseAgent):
        async def run(self, input, context=None, **kw):
            raise asyncio.CancelledError()

    async def _run():
        ex = AgentExecutor(retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0))
        with pytest.raises(asyncio.CancelledError):
            await ex.execute_tick(_CancelAgent(), "go")

    asyncio.run(_run())


def test_executor_cancelled_does_not_retry():
    """CancelledError must not trigger any retry — the agent is called exactly once."""
    attempts = []

    class _CancelAgent(BaseAgent):
        async def run(self, input, context=None, **kw):
            attempts.append(1)
            raise asyncio.CancelledError()

    async def _run():
        ex = AgentExecutor(retry=RetryPolicy(max_attempts=5, base_delay=0, max_delay=0))
        try:
            await ex.execute_tick(_CancelAgent(), "go")
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert len(attempts) == 1


# --- Task 2: Planner.plan() heavy/light routing -------------------------------

def test_planner_heavy_routes_to_plan_task_kind():
    """plan(heavy=True) should use TaskKind.PLAN, not EXECUTE."""
    from hive.llm.router import TaskKind

    class _KindCapture:
        def __init__(self):
            self.kinds = []

        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            self.kinds.append(kind)
            return CompletionResult(text="[]", model="fake")

    capture = _KindCapture()
    asyncio.run(Planner(capture).plan(["goal"], "context", heavy=True))
    assert TaskKind.PLAN in capture.kinds


def test_planner_light_routes_to_execute_task_kind():
    """plan(heavy=False) should use TaskKind.EXECUTE, not PLAN."""
    from hive.llm.router import TaskKind

    class _KindCapture:
        def __init__(self):
            self.kinds = []

        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            self.kinds.append(kind)
            return CompletionResult(text="[]", model="fake")

    capture = _KindCapture()
    asyncio.run(Planner(capture).plan(["goal"], "context", heavy=False))
    assert TaskKind.EXECUTE in capture.kinds
    assert TaskKind.PLAN not in capture.kinds


# --- Task 3: _safe_args() error handling --------------------------------------

def test_orchestrator_safe_args_malformed_json():
    """_safe_args() on malformed JSON returns {} without raising."""
    from hive.agents.orchestrator import _safe_args
    assert _safe_args('{"incomplete: 1') == {}


def test_orchestrator_safe_args_valid_json():
    """_safe_args() on valid JSON returns the parsed dict."""
    from hive.agents.orchestrator import _safe_args
    assert _safe_args('{"key": "value"}') == {"key": "value"}


def test_orchestrator_safe_args_empty_string():
    """_safe_args() on empty string returns {}."""
    from hive.agents.orchestrator import _safe_args
    assert _safe_args("") == {}


def test_orchestrator_safe_args_non_dict_json():
    """_safe_args() on a JSON non-dict (e.g. list) returns {}."""
    from hive.agents.orchestrator import _safe_args
    assert _safe_args("[1, 2, 3]") == {}


# --- New tests: executor max_iterations, orchestrator channel_hint ---------------

def test_agent_executor_max_iterations_respected():
    """A router that always returns a tool call stops after max_turns iterations."""
    from hive.agents.base import TerminalOutcome as TO

    # Each iteration produces a unique tool call so loop_guard's per-tool budget
    # is not what halts the loop — max_iterations is.
    calls_list = [
        ToolCall(id=f"c{i}", name="echo", arguments=json.dumps({"text": str(i)}))
        for i in range(50)
    ]
    router = _FakeRouter(
        [CompletionResult(text="", model="m", tool_calls=[c]) for c in calls_list]
    )
    # max_iterations=4, max_per_tool high enough not to interfere
    orch = ConversationOrchestrator(
        router, tools={"echo": _Echo()}, max_iterations=4, max_per_tool=100
    )
    res = asyncio.run(orch.ask("keep looping"))
    assert res.outcome == TO.MAX_TURNS
    # Exactly max_iterations turns were executed
    assert res.turns == 4


def test_orchestrator_channel_hint_passed_to_system_prompt():
    """orchestrator.ask(msg, channel_hint='telegram') includes '[Active surface: telegram]'."""
    captured_systems: list[str] = []

    class _CapturingRouter:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            captured_systems.append(system or "")
            return CompletionResult(text="ok", model="m")

    orch = ConversationOrchestrator(_CapturingRouter())
    asyncio.run(orch.ask("hello", channel_hint="telegram"))
    assert captured_systems, "router was never called"
    assert "[Active surface: telegram]" in captured_systems[0]
