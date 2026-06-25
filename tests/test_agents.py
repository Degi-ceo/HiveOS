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


# --- Additional agent tests --------------------------------------------------

def test_orchestrator_run_returns_agent_result():
    """ConversationOrchestrator.run() returns an AgentResult."""
    router = _FakeRouter(["hello"])
    orch = ConversationOrchestrator(router, tools={})
    result = asyncio.run(orch.run("test input", session_id="s1"))
    assert isinstance(result, AgentResult)


def test_orchestrator_run_outcome_completed():
    """Normal completion → outcome == COMPLETED."""
    router = _FakeRouter(["response"])
    orch = ConversationOrchestrator(router, tools={})
    result = asyncio.run(orch.run("hello", session_id="s2"))
    assert result.outcome == TerminalOutcome.COMPLETED


def test_orchestrator_ask_returns_content_string():
    """ask() returns an AgentResult with content field."""
    router = _FakeRouter(["hello world"])
    orch = ConversationOrchestrator(router, tools={})
    result = asyncio.run(orch.ask("test", session_id="s3"))
    assert "hello world" in result.content


def test_loop_guard_no_trip_on_diverse_tools():
    """LoopGuard never trips on diverse tool calls."""
    lg = LoopGuard()
    for name in ["search", "read", "write", "send", "fetch"]:
        result = lg.check(name)
        assert result is None


def test_agent_result_has_content():
    """AgentResult stores the content field."""
    r = AgentResult(content="answer", outcome=TerminalOutcome.COMPLETED)
    assert r.content == "answer"


def test_agent_result_outcome_default():
    """AgentResult defaults to COMPLETED outcome."""
    r = AgentResult(content="x")
    assert r.outcome == TerminalOutcome.COMPLETED


def test_terminal_outcome_has_failed():
    """TerminalOutcome.FAILED member exists."""
    assert hasattr(TerminalOutcome, "FAILED")


def test_terminal_outcome_has_cancelled():
    """TerminalOutcome.CANCELLED member exists."""
    assert hasattr(TerminalOutcome, "CANCELLED")


def test_planner_plan_with_heavy_false_uses_execute_kind():
    """Planner.plan(heavy=False) routes to EXECUTE kind."""
    from hive.llm.router import TaskKind
    kinds = []

    class _KindCapture:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            kinds.append(kind)
            return CompletionResult(text="[]", model="fake")

    asyncio.run(Planner(_KindCapture()).plan(["goal"], "ctx", heavy=False))
    assert TaskKind.EXECUTE in kinds


# --- Wave 3N additional tests ---------------------------------------------------

def test_loop_guard_identical_trips_at_max():
    """LoopGuard returns a non-None message once identical call count reaches max_identical."""
    lg = LoopGuard(max_identical=3)
    assert lg.check("toolX") is None
    assert lg.check("toolX") is None
    result = lg.check("toolX")
    assert result is not None


def test_loop_guard_per_tool_budget_trips():
    """LoopGuard trips when a single tool exceeds max_per_tool calls."""
    lg = LoopGuard(max_per_tool=2)
    assert lg.check("toolY") is None
    assert lg.check("toolY") is None
    result = lg.check("toolY")
    assert result is not None


def test_loop_guard_diverse_tools_never_trip():
    """Calling many distinct tools never triggers the loop guard."""
    lg = LoopGuard()
    for name in ["a", "b", "c", "d", "e", "f", "g"]:
        assert lg.check(name) is None


def test_loop_guard_different_args_count_separately():
    """Calls with different args do NOT count as identical — guard stays silent."""
    lg = LoopGuard(max_identical=2)
    assert lg.check("shellX", {"cmd": "ls"}) is None
    assert lg.check("shellX", {"cmd": "pwd"}) is None
    assert lg.check("shellX", {"cmd": "date"}) is None


def test_agent_result_stores_turns():
    """AgentResult.turns field stores the value passed at construction."""
    r = AgentResult(content="done", turns=7)
    assert r.turns == 7


def test_planner_plan_with_heavy_true_uses_plan_kind():
    """Planner.plan(heavy=True) routes to PLAN kind."""
    from hive.llm.router import TaskKind
    kinds = []

    class _KindCapture:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            kinds.append(kind)
            return CompletionResult(text="[]", model="fake")

    asyncio.run(Planner(_KindCapture()).plan(["goal"], "ctx", heavy=True))
    assert TaskKind.PLAN in kinds


# --- Wave 3T: 6 new tests -------------------------------------------------------

def test_loop_guard_reset_rearms_identical_check():
    """After reset(), the identical-call window is cleared so the guard does not trip early."""
    g = LoopGuard(max_identical=2)
    # reach the trip point
    g.check("toolX", {})
    assert g.check("toolX", {}) is not None  # trips at 2nd call
    # reset clears the window; first call after reset must be clean
    g.reset()
    assert g.check("toolX", {}) is None


def test_loop_guard_top_repeated_tools_n_zero_returns_one():
    """top_repeated_tools(n=0) returns exactly 1 entry because max(1, 0) == 1."""
    g = LoopGuard(max_per_tool=100)
    g.check("a", {})
    g.check("a", {})
    g.check("b", {})
    result = g.top_repeated_tools(n=0)
    assert len(result) == 1
    assert result[0] == ("a", 2)


def test_planner_returns_multiple_tasks():
    """Planner.plan() forwards the full list when the router returns 3 tasks."""
    import json as _json
    tasks = [
        {"task": "discover", "tool": "discover", "args": {}, "reason": "r1"},
        {"task": "search", "tool": "search", "args": {"q": "x"}, "reason": "r2"},
        {"task": "write", "tool": "write_file", "args": {}, "reason": "r3"},
    ]
    router = _FakeRouter([_json.dumps(tasks)])
    out = asyncio.run(Planner(router).plan(["goal1", "goal2"], "ctx"))
    assert len(out) == 3
    assert out[1]["tool"] == "search"


def test_agent_result_tool_results_list():
    """AgentResult stores a tool_results list and each entry is accessible."""
    from hive.core.types import ToolResult
    tr1 = ToolResult(tool_name="echo", content="first")
    tr2 = ToolResult(tool_name="search", content="second")
    r = AgentResult(content="done", tool_results=[tr1, tr2])
    assert len(r.tool_results) == 2
    assert r.tool_results[1].tool_name == "search"


def test_orchestrator_unknown_tool_falls_through():
    """A tool_call naming an unregistered tool is skipped; orchestrator still completes."""
    call = ToolCall(id="cx", name="__no_such_tool__", arguments=json.dumps({}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="recovered", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={})
    res = asyncio.run(orch.ask("hi"))
    assert res.outcome == TerminalOutcome.COMPLETED
    assert res.content == "recovered"


def test_orchestrator_accepts_tools_is_true():
    """ConversationOrchestrator.accepts_tools is True (inherits from ToolUsingAgent)."""
    router = _FakeRouter([CompletionResult(text="ok", model="m")])
    orch = ConversationOrchestrator(router)
    assert orch.accepts_tools is True


# --- Wave 4H: 8 new tests -------------------------------------------------------

def test_wave4h_orchestrator_max_iterations_one_no_tools():
    """max_iterations=1 with a plain answer completes in exactly 1 turn."""
    from hive.agents.base import TerminalOutcome as TO
    router = _FakeRouter([CompletionResult(text="one-shot", model="m")])
    orch = ConversationOrchestrator(router, max_iterations=1)
    res = asyncio.run(orch.ask("hi"))
    assert res.outcome == TO.COMPLETED
    assert res.turns == 1
    assert res.content == "one-shot"


def test_wave4h_orchestrator_max_iterations_one_hits_max_turns():
    """max_iterations=1 with continuous tool calls triggers MAX_TURNS after 1 turn."""
    from hive.agents.base import TerminalOutcome as TO
    calls = [ToolCall(id=f"c{i}", name="echo", arguments=json.dumps({"text": str(i)}))
             for i in range(10)]
    router = _FakeRouter([CompletionResult(text="", model="m", tool_calls=[c]) for c in calls])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()},
                                   max_iterations=1, max_per_tool=100)
    res = asyncio.run(orch.ask("go"))
    assert res.outcome == TO.MAX_TURNS
    assert res.turns == 1


def test_wave4h_terminal_outcome_base_all_values():
    """TerminalOutcome from base has COMPLETED, MAX_TURNS, LOOP_GUARD, TOOL_ERROR."""
    from hive.agents.base import TerminalOutcome as TO
    values = {e.value for e in TO}
    assert "completed" in values
    assert "max_turns" in values
    assert "loop_guard" in values
    assert "tool_error" in values


def test_wave4h_loop_guard_fires_inside_orchestrator_conversation():
    """Repeated identical tool calls inside an orchestrator turn trigger LOOP_GUARD."""
    from hive.agents.base import TerminalOutcome as TO
    identical_call = ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "same"}))
    router = _FakeRouter(
        [CompletionResult(text="", model="m", tool_calls=[identical_call])] * 20
    )
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()}, max_per_tool=1)
    res = asyncio.run(orch.ask("repeat"))
    assert res.outcome == TO.LOOP_GUARD


def test_wave4h_tool_call_result_stored_in_tool_results():
    """A successful tool call populates res.tool_results with the tool's output."""
    call = ToolCall(id="t1", name="echo", arguments=json.dumps({"text": "world"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="done", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo()})
    res = asyncio.run(orch.ask("call echo"))
    assert len(res.tool_results) == 1
    assert "world" in res.tool_results[0].content


def test_wave4h_agent_result_metadata_default_empty_dict():
    """AgentResult.metadata defaults to an empty dict when not provided."""
    r = AgentResult(content="ok")
    assert isinstance(r.metadata, dict)
    assert r.metadata == {}


def test_wave4h_agent_result_metadata_stores_custom_values():
    """AgentResult.metadata stores arbitrary key-value pairs passed at construction."""
    r = AgentResult(content="ok", metadata={"model": "m", "latency_ms": 42})
    assert r.metadata["model"] == "m"
    assert r.metadata["latency_ms"] == 42


def test_wave4h_executor_non_retryable_predicate_returns_failed_immediately():
    """AgentExecutor with is_retryable=lambda: False stops on first failure, attempts=1."""
    ex = AgentExecutor(
        retry=RetryPolicy(max_attempts=5, base_delay=0, max_delay=0),
        is_retryable=lambda _exc: False,
    )
    res = asyncio.run(ex.execute_tick(_Agent(fail_times=5), "go"))
    assert res.outcome is TerminalOutcome.FAILED
    assert res.attempts == 1


# --- Wave 4N: 8 new tests -------------------------------------------------------

class _EchoB(BaseTool):
    """Second echo tool that prefixes content with 'B:'."""
    spec = ToolSpec(name="echo_b", description="echo_b", parameters={"type": "object"})

    async def execute(self, **params):
        return ToolResult(tool_name="echo_b", content=f"B:{params.get('text', '')}")


def test_wave4n_multiple_tool_calls_per_turn_both_executed():
    """Two tool calls in a single model response are both dispatched and results collected."""
    call_a = ToolCall(id="a1", name="echo", arguments=json.dumps({"text": "alpha"}))
    call_b = ToolCall(id="b1", name="echo_b", arguments=json.dumps({"text": "beta"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call_a, call_b]),
        CompletionResult(text="finished", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo(), "echo_b": _EchoB()})
    res = asyncio.run(orch.ask("call both"))
    assert res.outcome == TerminalOutcome.COMPLETED
    assert len(res.tool_results) == 2


def test_wave4n_tool_results_order_matches_call_order():
    """tool_results are appended in the same order the tool calls were issued."""
    call_a = ToolCall(id="a2", name="echo", arguments=json.dumps({"text": "first"}))
    call_b = ToolCall(id="b2", name="echo_b", arguments=json.dumps({"text": "second"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call_a, call_b]),
        CompletionResult(text="ok", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo(), "echo_b": _EchoB()})
    res = asyncio.run(orch.ask("order check"))
    assert "echoed:first" in res.tool_results[0].content
    assert "B:second" in res.tool_results[1].content


def test_wave4n_tool_results_reversed_call_order():
    """Reversed call order (echo_b first, echo second) is reflected in tool_results."""
    call_b = ToolCall(id="rb1", name="echo_b", arguments=json.dumps({"text": "uno"}))
    call_a = ToolCall(id="ra1", name="echo", arguments=json.dumps({"text": "dos"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call_b, call_a]),
        CompletionResult(text="done", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo(), "echo_b": _EchoB()})
    res = asyncio.run(orch.ask("reverse order"))
    assert "B:uno" in res.tool_results[0].content
    assert "echoed:dos" in res.tool_results[1].content


def test_wave4n_agent_result_non_default_outcome_loop_guard():
    """AgentResult constructed with LOOP_GUARD outcome stores that value."""
    from hive.agents.base import TerminalOutcome as BaseTO
    r = AgentResult(content="stopped", outcome=BaseTO.LOOP_GUARD)
    assert r.outcome == BaseTO.LOOP_GUARD
    assert r.outcome != BaseTO.COMPLETED


def test_wave4n_agent_result_non_default_outcome_max_turns():
    """AgentResult constructed with MAX_TURNS outcome stores that value."""
    from hive.agents.base import TerminalOutcome as BaseTO
    r = AgentResult(content="timed out", outcome=BaseTO.MAX_TURNS)
    assert r.outcome == BaseTO.MAX_TURNS


def test_wave4n_orchestrator_metadata_not_present_in_result():
    """Orchestrator result metadata is an empty dict (no custom metadata injected)."""
    router = _FakeRouter([CompletionResult(text="response", model="m")])
    orch = ConversationOrchestrator(router)
    res = asyncio.run(orch.ask("hi"))
    assert isinstance(res.metadata, dict)


def test_wave4n_orchestrator_custom_session_id_isolates_history():
    """Two separate session IDs each get independent message histories."""
    from hive.context.session_store import SessionStore
    import tempfile, os
    tmp = tempfile.mkdtemp()
    store = SessionStore(os.path.join(tmp, "iso.sqlite"))
    router_a = _FakeRouter([CompletionResult(text="reply-A", model="m")])
    orch_a = ConversationOrchestrator(router_a, session_store=store)
    asyncio.run(orch_a.ask("msg-A", session_id="sess-A"))

    router_b = _FakeRouter([CompletionResult(text="reply-B", model="m")])
    orch_b = ConversationOrchestrator(router_b, session_store=store)
    asyncio.run(orch_b.ask("msg-B", session_id="sess-B"))

    msgs_a = [m.content for m in store.messages("sess-A")]
    msgs_b = [m.content for m in store.messages("sess-B")]
    assert "msg-A" in msgs_a and "reply-A" in msgs_a
    assert "msg-B" in msgs_b and "reply-B" in msgs_b
    assert "msg-B" not in msgs_a
    assert "msg-A" not in msgs_b


def test_wave4n_multiple_tool_calls_two_turns_total_results_accumulate():
    """Tool results from turn 1 (2 calls) then turn 2 (1 call) accumulate to 3 total."""
    call_a = ToolCall(id="t1a", name="echo", arguments=json.dumps({"text": "x"}))
    call_b = ToolCall(id="t1b", name="echo_b", arguments=json.dumps({"text": "y"}))
    call_c = ToolCall(id="t2a", name="echo", arguments=json.dumps({"text": "z"}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call_a, call_b]),
        CompletionResult(text="", model="m", tool_calls=[call_c]),
        CompletionResult(text="all done", model="m"),
    ])
    orch = ConversationOrchestrator(router, tools={"echo": _Echo(), "echo_b": _EchoB()})
    res = asyncio.run(orch.ask("multi-turn tools"))
    assert res.outcome == TerminalOutcome.COMPLETED
    assert len(res.tool_results) == 3


# --- Wave 5: lift orchestrator.py coverage from 91% to 100% ------------------

def test_orchestrator_compacts_history_when_summarizer_set_and_long():
    """When summarizer is wired + history > compact_trigger, compaction runs (line 110)."""
    from hive.context.session_store import SessionStore
    from hive.core.types import Message, Role
    import tempfile, os, json as _json

    tmp = tempfile.mkdtemp()
    store = SessionStore(os.path.join(tmp, "s.sqlite"))
    # Seed 30 turns so we exceed the default compact_trigger=24.
    for i in range(30):
        store.append("s1", Role.USER, f"msg {i}")

    calls = {"n": 0}
    async def fake_summarize(messages, system):
        calls["n"] += 1
        return "compacted"
    router = _FakeRouter([CompletionResult(text="done", model="m")])
    orch = ConversationOrchestrator(router, session_store=store, summarizer=fake_summarize)
    res = asyncio.run(orch.ask("hi", session_id="s1"))
    assert res.content == "done"
    assert calls["n"] >= 1    # compact() was actually invoked


def test_orchestrator_max_turns_uses_planner_hint_when_no_results():
    """At max turns with planner wired and zero tool_results, hint is appended (149-159)."""
    from unittest.mock import MagicMock
    from hive.tools.executor import DispatchStatus
    from hive.agents.base import TerminalOutcome as _BaseOutcome

    # Fake executor that always errors — keeps tool_results empty (only appended on OK).
    fake_exec = MagicMock()
    dispatch = MagicMock()
    dispatch.status = DispatchStatus.ERROR
    dispatch.error = "boom"
    async def _execute(name, args, reason):
        return dispatch
    fake_exec.execute = _execute

    call1 = ToolCall(id="c1", name="bad", arguments=json.dumps({}))
    call2 = ToolCall(id="c2", name="bad", arguments=json.dumps({}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call1]),
        CompletionResult(text="", model="m", tool_calls=[call2]),
    ])

    planner = MagicMock()
    async def _plan(goals, context):
        return [{"task": "check logs", "tool": "shell"},
                {"task": "read README", "tool": "read_file"}]
    planner.plan = _plan

    orch = ConversationOrchestrator(router, tool_executor=fake_exec,
                                    max_iterations=2, planner=planner)
    res = asyncio.run(orch.ask("loop"))
    assert res.outcome == _BaseOutcome.MAX_TURNS
    assert "Suggested next steps" in res.content
    assert "check logs" in res.content


def test_orchestrator_planner_hint_swallows_exception():
    """If planner.plan() raises, the hint is skipped but the run completes (158-159)."""
    from unittest.mock import MagicMock
    from hive.tools.executor import DispatchStatus
    from hive.agents.base import TerminalOutcome as _BaseOutcome

    fake_exec = MagicMock()
    dispatch = MagicMock()
    dispatch.status = DispatchStatus.ERROR
    async def _execute(name, args, reason):
        return dispatch
    fake_exec.execute = _execute

    call = ToolCall(id="c1", name="bad", arguments=json.dumps({}))
    router = _FakeRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),
        CompletionResult(text="", model="m", tool_calls=[call]),
    ])

    planner = MagicMock()
    async def _plan(*a, **kw):
        raise RuntimeError("planner down")
    planner.plan = _plan

    orch = ConversationOrchestrator(router, tool_executor=fake_exec,
                                    max_iterations=2, planner=planner)
    res = asyncio.run(orch.ask("loop"))
    # The exception was swallowed — we still got a MAX_TURNS result, no crash.
    assert res.outcome == _BaseOutcome.MAX_TURNS
    assert "Suggested next steps" not in res.content


def test_orchestrator_dispatch_returns_error_message_when_executor_errors():
    """_dispatch() returns '[tool error: ...]' when executor returns ERROR (line 173)."""
    from unittest.mock import AsyncMock, MagicMock
    from hive.tools.executor import DispatchStatus

    fake_exec = MagicMock()
    dispatch = MagicMock()
    dispatch.status = DispatchStatus.ERROR
    dispatch.error = "kaboom"
    async def _execute(name, args, reason):
        return dispatch
    fake_exec.execute = _execute

    router = _FakeRouter([
        CompletionResult(text="", model="m",
                          tool_calls=[ToolCall(id="c1", name="bad",
                                               arguments=json.dumps({}))]),
        CompletionResult(text="got the error", model="m"),
    ])
    orch = ConversationOrchestrator(router, tool_executor=fake_exec)
    res = asyncio.run(orch.ask("please fail"))
    assert res.content == "got the error"
    # The tool result message reached the model on the second turn.
    second_msgs = router.calls[1]["messages"]
    assert any("tool error: kaboom" in m.content for m in second_msgs)
