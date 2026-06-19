"""
test_m10_agents.py — M10-d: specialist sub-agents.

Tests:
  - All 5 agent YAML/MD files parse correctly (frontmatter + body present)
  - delegate_named resolves agents from the registry
  - get_agent_factory raises KeyError for unknown names
  - HiveOS.agents_registry contains all 5 specialist names after build
"""
from __future__ import annotations

import re
from pathlib import Path

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS

_AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
_EXPECTED_AGENTS = {
    "researcher", "coder", "reviewer", "memory-keeper", "security-reviewer",
}


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="[]", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# Agent file parsing
# ---------------------------------------------------------------------------

def test_agents_dir_exists():
    assert _AGENTS_DIR.is_dir(), f".claude/agents/ not found at {_AGENTS_DIR}"


def test_all_agent_files_present():
    found = {p.stem for p in _AGENTS_DIR.glob("*.md")}
    missing = _EXPECTED_AGENTS - found
    assert not missing, f"missing agent files: {missing}"


def test_agent_files_have_frontmatter():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert content.startswith("---"), f"{agent_file.name} missing YAML frontmatter"
        assert "---" in content[3:], f"{agent_file.name} frontmatter not closed"


def test_agent_files_have_name_field():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert re.search(r"^name:", content, re.MULTILINE), \
            f"{agent_file.name} missing 'name:' in frontmatter"


def test_agent_files_have_description():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert re.search(r"^description:", content, re.MULTILINE), \
            f"{agent_file.name} missing 'description:' in frontmatter"


def test_agent_files_have_body():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        parts = content.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else ""
        assert len(body) > 50, f"{agent_file.name} body is too short (< 50 chars)"


# ---------------------------------------------------------------------------
# Named registry — delegate module
# ---------------------------------------------------------------------------

def test_delegate_named_registry_resolves(tmp_path):
    from hive.agents.delegate import get_agent_factory
    _make_hive(tmp_path)  # populates _AGENT_REGISTRY as side-effect
    for name in _EXPECTED_AGENTS:
        factory = get_agent_factory(name)
        assert callable(factory), f"factory for {name!r} is not callable"


def test_get_agent_factory_raises_for_unknown():
    from hive.agents.delegate import get_agent_factory
    try:
        get_agent_factory("nonexistent_agent_xyz")
        assert False, "should have raised KeyError"
    except KeyError:
        pass


# ---------------------------------------------------------------------------
# HiveOS.agents_registry
# ---------------------------------------------------------------------------

def test_hive_agents_registry_populated(tmp_path):
    hive = _make_hive(tmp_path)
    assert hasattr(hive, "agents_registry")
    for name in _EXPECTED_AGENTS:
        assert name in hive.agents_registry, f"{name!r} missing from agents_registry"


def test_hive_agents_registry_values_callable(tmp_path):
    hive = _make_hive(tmp_path)
    for name, factory in hive.agents_registry.items():
        assert callable(factory), f"factory for {name!r} is not callable"


# --- Agent registry extra tests ---------------------------------------------------

def test_agent_frontmatter_model_field_valid():
    """Agent frontmatter model field, if present, should be a valid-looking model string."""
    import os, re
    agents_dir = os.path.join(os.path.dirname(__file__), "..", ".claude", "agents")
    agents_dir = os.path.realpath(agents_dir)
    for fname in os.listdir(agents_dir):
        if not fname.endswith(".md"):
            continue
        content = open(os.path.join(agents_dir, fname)).read()
        if "model:" in content[:500]:
            # If model field exists, it should look like a model string (not empty)
            m = re.search(r"^model:\s*(\S+)", content[:500], re.MULTILINE)
            if m:
                assert len(m.group(1)) > 3, f"{fname}: model field looks empty"


def test_all_agent_files_readable_utf8():
    """All .claude/agents/*.md files must be readable as UTF-8."""
    import os
    agents_dir = os.path.join(os.path.dirname(__file__), "..", ".claude", "agents")
    agents_dir = os.path.realpath(agents_dir)
    for fname in os.listdir(agents_dir):
        if fname.endswith(".md"):
            path = os.path.join(agents_dir, fname)
            open(path, encoding="utf-8").read()  # would raise on encoding error


def test_register_and_get_agent_factory():
    from hive.agents.delegate import register_agent, get_agent_factory

    def _factory():
        pass

    register_agent("test-extra-agent", _factory)
    assert get_agent_factory("test-extra-agent") is _factory


# ---------------------------------------------------------------------------
# New tests — frontmatter completeness, registry callable, delegate_named,
# per-name specialist presence, and name-filename alignment
# ---------------------------------------------------------------------------

def test_all_agents_have_description_field():
    """Every .claude/agents/*.md file must have a 'description:' key in its frontmatter."""
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text(encoding="utf-8")
        assert re.search(r"^description:", content, re.MULTILINE), (
            f"{agent_file.name} is missing 'description:' in frontmatter"
        )


def test_agent_factory_register_returns_callable():
    """A freshly registered factory must be retrieved as the exact same callable."""
    from hive.agents.delegate import register_agent, get_agent_factory

    def my_factory():
        return None

    register_agent("_test_callable_check", my_factory)
    retrieved = get_agent_factory("_test_callable_check")
    assert callable(retrieved), "retrieved factory is not callable"
    assert retrieved is my_factory, "retrieved factory is not the same object"


def test_delegate_named_returns_list(tmp_path):
    """delegate_named with a monkeypatched factory must return a list of AgentResult."""
    import asyncio
    from hive.agents.delegate import register_agent, delegate_named
    from hive.agents.base import AgentResult
    from hive.agents.executor import AgentExecutor, TerminalOutcome, TickResult

    class _FakeAgent:
        agent_id = "fake"
        accepts_tools = False

        async def run(self, input: str, context=None, **kw) -> AgentResult:
            return AgentResult(content=f"done:{input}")

    class _FakeExecutor(AgentExecutor):
        async def execute_tick(self, agent, task: str, context=None):  # type: ignore[override]
            result = await agent.run(task)
            return TickResult(
                outcome=TerminalOutcome.COMPLETED,
                result=result,
                error=None,
            )

    register_agent("_test_fake_agent", _FakeAgent)
    results = asyncio.run(
        delegate_named(["task1", "task2"], "_test_fake_agent",
                       executor=_FakeExecutor())
    )
    assert isinstance(results, list), "delegate_named did not return a list"
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    for r in results:
        assert isinstance(r, AgentResult)


def test_named_specialist_researcher_registered(tmp_path):
    """'researcher' must be present in the agent registry after HiveOS.build()."""
    hive = _make_hive(tmp_path)
    assert "researcher" in hive.agents_registry, \
        "'researcher' not found in agents_registry"
    assert callable(hive.agents_registry["researcher"])


def test_named_specialist_coder_registered(tmp_path):
    """'coder' must be present in the agent registry after HiveOS.build()."""
    hive = _make_hive(tmp_path)
    assert "coder" in hive.agents_registry, \
        "'coder' not found in agents_registry"
    assert callable(hive.agents_registry["coder"])


def test_agent_spec_name_matches_filename():
    """The 'name:' value in each agent's frontmatter must match the file stem."""
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text(encoding="utf-8")
        m = re.search(r"^name:\s*(\S+)", content, re.MULTILINE)
        assert m, f"{agent_file.name} has no 'name:' field"
        assert m.group(1) == agent_file.stem, (
            f"{agent_file.name}: frontmatter name={m.group(1)!r} "
            f"does not match stem={agent_file.stem!r}"
        )


# --- Wave 3K additional tests -------------------------------------------------

def test_agent_executor_tick_result_completed():
    """TickResult with COMPLETED outcome stores the result."""
    from hive.agents.executor import TickResult
    from hive.agents.executor import TerminalOutcome
    from hive.agents.base import AgentResult

    r = AgentResult(content="done")
    tick = TickResult(outcome=TerminalOutcome.COMPLETED, result=r)
    assert tick.outcome == TerminalOutcome.COMPLETED
    assert tick.result is r


def test_agent_executor_tick_result_failed():
    """TickResult with FAILED outcome stores an error string."""
    from hive.agents.executor import TickResult, TerminalOutcome

    tick = TickResult(outcome=TerminalOutcome.FAILED, error="something broke")
    assert tick.outcome == TerminalOutcome.FAILED
    assert tick.error == "something broke"


def test_named_specialist_security_reviewer_registered(tmp_path):
    """'security-reviewer' must be in agents_registry after HiveOS.build()."""
    def _make():
        from unittest.mock import patch
        with patch("hive.runtime.build_mnemosyne_provider", return_value=None):
            return HiveOS.build(
                HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                router=_ScriptRouter(),
            )
    hive = _make()
    assert "security-reviewer" in hive.agents_registry


def test_named_specialist_memory_keeper_registered(tmp_path):
    """'memory-keeper' must be in agents_registry after HiveOS.build()."""
    from unittest.mock import patch

    with patch("hive.runtime.build_mnemosyne_provider", return_value=None):
        hive = HiveOS.build(
            HiveConfig.from_env(root=tmp_path, load_dotenv=False),
            router=_ScriptRouter(),
        )
    assert "memory-keeper" in hive.agents_registry


def test_all_agents_have_tools_key_in_frontmatter():
    """Every .claude/agents/*.md frontmatter should contain a 'tools:' field."""
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text(encoding="utf-8")
        assert re.search(r"^tools:", content, re.MULTILINE), (
            f"{agent_file.name} is missing 'tools:' in frontmatter"
        )


def test_get_agent_factory_raises_keyerror_for_unknown():
    """get_agent_factory with an unknown name raises KeyError."""
    import pytest
    from hive.agents.delegate import get_agent_factory
    with pytest.raises(KeyError):
        get_agent_factory("nonexistent-agent-xyz")


# --- Wave 4 additional tests (6) -----------------------------------------------

def test_tick_result_cancelled_outcome():
    """TickResult supports the CANCELLED terminal outcome."""
    from hive.agents.executor import TickResult, TerminalOutcome
    tick = TickResult(outcome=TerminalOutcome.CANCELLED)
    assert tick.outcome == TerminalOutcome.CANCELLED
    assert tick.result is None
    assert tick.error is None


def test_tick_result_attempts_field():
    """TickResult stores the attempts count correctly."""
    from hive.agents.executor import TickResult, TerminalOutcome
    tick = TickResult(outcome=TerminalOutcome.COMPLETED, attempts=2)
    assert tick.attempts == 2


def test_agent_executor_completes_on_successful_agent():
    """AgentExecutor.execute_tick must return COMPLETED when agent.run() succeeds."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext

    class _GoodAgent(BaseAgent):
        agent_id = "good"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content=f"processed:{input}")

    ex = AgentExecutor()
    tick = asyncio.run(ex.execute_tick(_GoodAgent(), "hello"))
    assert tick.outcome == TerminalOutcome.COMPLETED
    assert tick.result is not None
    assert tick.result.content == "processed:hello"


def test_named_specialist_reviewer_registered(tmp_path):
    """'reviewer' must be present in the agents_registry after HiveOS.build()."""
    hive = _make_hive(tmp_path)
    assert "reviewer" in hive.agents_registry, \
        "'reviewer' not found in agents_registry"
    assert callable(hive.agents_registry["reviewer"])


def test_retry_policy_default_max_attempts():
    """RetryPolicy default max_attempts must be 3."""
    from hive.agents.executor import RetryPolicy
    policy = RetryPolicy()
    assert policy.max_attempts == 3


def test_agent_result_default_outcome_is_completed():
    """AgentResult default outcome must be TerminalOutcome.COMPLETED."""
    from hive.agents.base import AgentResult
    from hive.agents.executor import TerminalOutcome
    r = AgentResult(content="done")
    assert r.outcome == TerminalOutcome.COMPLETED


# --- Wave 5 additional tests (6) -----------------------------------------------

def test_executor_failed_outcome_when_agent_always_raises():
    """AgentExecutor must return FAILED after all retry attempts are exhausted."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy

    class _AlwaysFail(BaseAgent):
        agent_id = "always-fail"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            raise RuntimeError("permanent failure")

    ex = AgentExecutor(retry=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0))
    tick = asyncio.run(ex.execute_tick(_AlwaysFail(), "input"))
    assert tick.outcome == TerminalOutcome.FAILED
    assert tick.result is None
    assert "permanent failure" in tick.error


def test_executor_records_correct_attempt_count_on_failure():
    """AgentExecutor.attempts must equal max_attempts when all retries fail."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy

    class _FailEveryTime(BaseAgent):
        agent_id = "fail-every-time"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            raise ValueError("fail")

    ex = AgentExecutor(retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0))
    tick = asyncio.run(ex.execute_tick(_FailEveryTime(), "x"))
    assert tick.attempts == 3


def test_executor_non_retryable_stops_after_first_attempt():
    """When is_retryable returns False, executor must stop after exactly 1 attempt."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy

    class _FatalAgent(BaseAgent):
        agent_id = "fatal"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            raise TypeError("fatal error")

    ex = AgentExecutor(
        retry=RetryPolicy(max_attempts=5, base_delay=0, max_delay=0),
        is_retryable=lambda _exc: False,
    )
    tick = asyncio.run(ex.execute_tick(_FatalAgent(), "x"))
    assert tick.outcome == TerminalOutcome.FAILED
    assert tick.attempts == 1


def test_executor_succeeds_after_transient_failures():
    """Executor must succeed when agent recovers within max_attempts."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy

    call_log: list[int] = []

    class _FlakyAgent(BaseAgent):
        agent_id = "flaky-recover"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            call_log.append(1)
            if len(call_log) < 3:
                raise RuntimeError(f"transient #{len(call_log)}")
            return AgentResult(content="recovered")

    ex = AgentExecutor(retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0))
    tick = asyncio.run(ex.execute_tick(_FlakyAgent(), "work"))
    assert tick.outcome == TerminalOutcome.COMPLETED
    assert tick.result is not None
    assert tick.result.content == "recovered"
    assert tick.attempts == 3


def test_tick_result_failed_stores_error_message():
    """TickResult with FAILED outcome stores the error string."""
    from hive.agents.executor import TickResult, TerminalOutcome
    tick = TickResult(outcome=TerminalOutcome.FAILED, attempts=1, error="something broke")
    assert tick.outcome == TerminalOutcome.FAILED
    assert tick.error == "something broke"
    assert tick.result is None


def test_executor_completed_tick_has_no_error():
    """TickResult returned from a successful execute_tick must have error=None."""
    import asyncio
    from hive.agents.executor import AgentExecutor, TerminalOutcome
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy

    class _OkAgent(BaseAgent):
        agent_id = "ok-agent"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content="all good")

    ex = AgentExecutor(retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0))
    tick = asyncio.run(ex.execute_tick(_OkAgent(), "query"))
    assert tick.outcome == TerminalOutcome.COMPLETED
    assert tick.error is None
    assert tick.result.content == "all good"


# --- Wave 3V additional tests (8) -----------------------------------------------

def test_wave3v_agent_context_metadata_default_empty():
    """AgentContext.metadata must default to an empty dict."""
    from hive.agents.base import AgentContext
    ctx = AgentContext()
    assert isinstance(ctx.metadata, dict)
    assert len(ctx.metadata) == 0


def test_wave3v_agent_context_tools_default_empty_list():
    """AgentContext.tools must default to an empty list."""
    from hive.agents.base import AgentContext
    ctx = AgentContext()
    assert isinstance(ctx.tools, list)
    assert ctx.tools == []


def test_wave3v_agent_result_turns_default_zero():
    """AgentResult.turns must default to 0."""
    from hive.agents.base import AgentResult
    r = AgentResult(content="x")
    assert r.turns == 0


def test_wave3v_agent_result_metadata_default_empty():
    """AgentResult.metadata must default to an empty dict."""
    from hive.agents.base import AgentResult
    r = AgentResult(content="x")
    assert isinstance(r.metadata, dict)
    assert r.metadata == {}


def test_wave3v_delegate_runs_subtasks_in_order():
    """delegate() preserves input order in the returned results."""
    import asyncio
    from hive.agents.delegate import delegate
    from hive.agents.base import AgentResult, BaseAgent, AgentContext
    from hive.llm.failover import RetryPolicy
    from hive.agents.executor import AgentExecutor

    class _EchoAgent(BaseAgent):
        agent_id = "echo"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content=f"echo:{input}")

    results = asyncio.run(
        delegate(
            ["a", "b", "c"],
            agent_factory=_EchoAgent,
            executor=AgentExecutor(retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0)),
        )
    )
    assert [r.content for r in results] == ["echo:a", "echo:b", "echo:c"]


def test_wave3v_base_agent_accepts_tools_default_false():
    """BaseAgent.accepts_tools class attribute defaults to False."""
    from hive.agents.base import BaseAgent, AgentResult, AgentContext

    class _MinimalAgent(BaseAgent):
        agent_id = "minimal"

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content="ok")

    assert _MinimalAgent.accepts_tools is False


def test_wave3v_tool_using_agent_accepts_tools_true():
    """ToolUsingAgent.accepts_tools must be True."""
    from hive.agents.base import ToolUsingAgent
    assert ToolUsingAgent.accepts_tools is True


def test_wave3v_base_terminal_outcome_values():
    """base.TerminalOutcome must include COMPLETED, MAX_TURNS, LOOP_GUARD, TOOL_ERROR."""
    from hive.agents.base import TerminalOutcome
    assert TerminalOutcome.COMPLETED.value == "completed"
    assert TerminalOutcome.MAX_TURNS.value == "max_turns"
    assert TerminalOutcome.LOOP_GUARD.value == "loop_guard"
    assert TerminalOutcome.TOOL_ERROR.value == "tool_error"


def test_wave3v_agent_registry_contains_all_expected(tmp_path):
    """agents_registry after HiveOS.build() contains exactly all 5 expected agents."""
    hive = _make_hive(tmp_path)
    for name in _EXPECTED_AGENTS:
        assert name in hive.agents_registry, f"missing: {name!r}"
    assert len(hive.agents_registry) >= len(_EXPECTED_AGENTS)


# --- Wave 3Z-A additional tests (8) --------------------------------------------

def test_wave3z_agent_context_memory_results_default_empty():
    from hive.agents.base import AgentContext
    ctx = AgentContext()
    assert isinstance(ctx.memory_results, list)
    assert ctx.memory_results == []


def test_wave3z_agent_context_stores_tools_list():
    from hive.agents.base import AgentContext
    ctx = AgentContext(tools=["read_file", "shell"])
    assert ctx.tools == ["read_file", "shell"]


def test_wave3z_agent_result_tool_results_default_empty():
    from hive.agents.base import AgentResult
    r = AgentResult(content="hi")
    assert isinstance(r.tool_results, list)
    assert r.tool_results == []


def test_wave3z_all_specialist_factories_produce_run_method(tmp_path):
    hive = _make_hive(tmp_path)
    for name, factory in hive.agents_registry.items():
        agent = factory()
        assert callable(getattr(agent, "run", None)), f"{name}: run() not callable"


def test_wave3z_delegate_named_single_task_returns_one_result(tmp_path):
    import asyncio
    from hive.agents.delegate import register_agent, delegate_named
    from hive.agents.base import AgentResult, BaseAgent, AgentContext

    class _SingleEcho(BaseAgent):
        agent_id = "single-echo"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content=f"single:{input}")

    register_agent("_test_single_echo", _SingleEcho)
    results = asyncio.run(delegate_named(["only"], "_test_single_echo"))
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0].content == "single:only"


def test_wave3z_agent_result_stores_content():
    from hive.agents.base import AgentResult
    r = AgentResult(content="hello world")
    assert r.content == "hello world"


def test_wave3z_agent_context_metadata_stores_values():
    from hive.agents.base import AgentContext
    ctx = AgentContext(metadata={"priority": "high", "retries": 3})
    assert ctx.metadata["priority"] == "high"
    assert ctx.metadata["retries"] == 3


def test_wave3z_executor_terminal_outcome_failed_value():
    from hive.agents.executor import TerminalOutcome
    assert TerminalOutcome.FAILED.value == "failed"


# --- Wave 4E additional tests ---------------------------------------------------

def test_wave4e_agent_context_conversation_add_message():
    """AgentContext.conversation.add() appends a Message to the conversation."""
    from hive.agents.base import AgentContext
    from hive.core.types import Conversation, Message, Role
    ctx = AgentContext()
    assert len(ctx.conversation.messages) == 0
    ctx.conversation.add(Message(role=Role.USER, content="hello"))
    assert len(ctx.conversation.messages) == 1
    assert ctx.conversation.messages[0].content == "hello"


def test_wave4e_agent_context_conversation_multiple_messages():
    """AgentContext.conversation stores multiple messages in insertion order."""
    from hive.agents.base import AgentContext
    from hive.core.types import Message, Role
    ctx = AgentContext()
    ctx.conversation.add(Message(role=Role.USER, content="first"))
    ctx.conversation.add(Message(role=Role.ASSISTANT, content="second"))
    assert len(ctx.conversation.messages) == 2
    assert ctx.conversation.messages[1].content == "second"


def test_wave4e_agent_result_outcome_max_turns():
    """AgentResult with outcome=MAX_TURNS stores that outcome."""
    from hive.agents.base import AgentResult, TerminalOutcome
    r = AgentResult(content="stopped", outcome=TerminalOutcome.MAX_TURNS)
    assert r.outcome == TerminalOutcome.MAX_TURNS
    assert r.outcome == "max_turns"


def test_wave4e_agent_result_outcome_loop_guard():
    """AgentResult with outcome=LOOP_GUARD stores that outcome."""
    from hive.agents.base import AgentResult, TerminalOutcome
    r = AgentResult(content="loop", outcome=TerminalOutcome.LOOP_GUARD)
    assert r.outcome == TerminalOutcome.LOOP_GUARD
    assert r.outcome == "loop_guard"


def test_wave4e_agent_result_outcome_tool_error():
    """AgentResult with outcome=TOOL_ERROR stores that outcome."""
    from hive.agents.base import AgentResult, TerminalOutcome
    r = AgentResult(content="error", outcome=TerminalOutcome.TOOL_ERROR)
    assert r.outcome == TerminalOutcome.TOOL_ERROR
    assert r.outcome == "tool_error"


def test_wave4e_delegate_named_three_tasks():
    """delegate_named with 3 tasks returns 3 AgentResult objects."""
    import asyncio
    from hive.agents.delegate import register_agent, delegate_named
    from hive.agents.base import AgentResult, BaseAgent, AgentContext

    class _TripleEcho(BaseAgent):
        agent_id = "triple-echo"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content=f"triple:{input}")

    register_agent("_test_triple_echo", _TripleEcho)
    results = asyncio.run(delegate_named(["x", "y", "z"], "_test_triple_echo"))
    assert len(results) == 3
    assert results[0].content == "triple:x"
    assert results[1].content == "triple:y"
    assert results[2].content == "triple:z"


def test_wave4e_terminal_outcome_str_comparison():
    """TerminalOutcome values compare equal to plain strings (str enum property)."""
    from hive.agents.base import TerminalOutcome
    assert TerminalOutcome.COMPLETED == "completed"
    assert TerminalOutcome.MAX_TURNS == "max_turns"
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"
    assert TerminalOutcome.TOOL_ERROR == "tool_error"


def test_wave4e_terminal_outcome_not_equal_wrong_value():
    """TerminalOutcome members are not equal to wrong string values."""
    from hive.agents.base import TerminalOutcome
    assert TerminalOutcome.COMPLETED != "max_turns"
    assert TerminalOutcome.MAX_TURNS != "completed"


# --- Wave 4K-B additional tests (8) -------------------------------------------

def test_wave4k_agent_context_prepopulated_tools():
    """AgentContext initialized with a tools list stores them verbatim."""
    from hive.agents.base import AgentContext
    tools = ["search", "read_file", "shell"]
    ctx = AgentContext(tools=tools)
    assert ctx.tools == tools
    assert len(ctx.tools) == 3
    assert "read_file" in ctx.tools


def test_wave4k_agent_result_custom_metadata_keys():
    """AgentResult.metadata stores arbitrary key-value pairs set at construction."""
    from hive.agents.base import AgentResult
    r = AgentResult(content="ok", metadata={"source": "web", "score": 0.95, "retries": 2})
    assert r.metadata["source"] == "web"
    assert r.metadata["score"] == 0.95
    assert r.metadata["retries"] == 2


def test_wave4k_terminal_outcome_enum_iteration():
    """Iterating TerminalOutcome yields all expected members in any order."""
    from hive.agents.base import TerminalOutcome
    values = {m.value for m in TerminalOutcome}
    assert "completed" in values
    assert "max_turns" in values
    assert "loop_guard" in values
    assert "tool_error" in values


def test_wave4k_base_agent_custom_agent_id():
    """Subclassing BaseAgent and setting agent_id exposes that id on the class."""
    from hive.agents.base import BaseAgent, AgentContext, AgentResult

    class _CustomAgent(BaseAgent):
        agent_id = "my-custom-agent"
        accepts_tools = False

        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content="custom")

    agent = _CustomAgent()
    assert agent.agent_id == "my-custom-agent"
    assert _CustomAgent.agent_id == "my-custom-agent"


def test_wave4k_base_agent_id_inherited_when_not_overridden():
    """BaseAgent.agent_id defaults to 'base' when subclass does not set it."""
    from hive.agents.base import BaseAgent, AgentContext, AgentResult

    class _NakedAgent(BaseAgent):
        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content="naked")

    assert _NakedAgent.agent_id == "base"


def test_wave4k_delegate_named_unknown_raises_keyerror():
    """delegate_named with a name not in the registry raises KeyError synchronously."""
    import asyncio
    from hive.agents.delegate import delegate_named

    try:
        asyncio.run(delegate_named(["task"], "totally-unknown-agent-wave4k"))
        assert False, "should have raised KeyError"
    except KeyError as exc:
        assert "totally-unknown-agent-wave4k" in str(exc)


def test_wave4k_delegate_named_error_message_lists_available():
    """KeyError from delegate_named includes the word 'available' for discoverability."""
    import asyncio
    from hive.agents.delegate import delegate_named, register_agent
    from hive.agents.base import BaseAgent, AgentContext, AgentResult

    class _Dummy(BaseAgent):
        agent_id = "dummy-wave4k"
        accepts_tools = False
        async def run(self, input: str, context: AgentContext | None = None) -> AgentResult:
            return AgentResult(content="dummy")

    register_agent("dummy-wave4k-listed", _Dummy)
    try:
        asyncio.run(delegate_named(["t"], "no-such-agent-wave4k-xyz"))
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "available" in str(exc).lower()


def test_wave4k_agent_result_metadata_mutation_does_not_affect_other_instances():
    """Mutating one AgentResult's metadata dict does not affect another instance's."""
    from hive.agents.base import AgentResult
    r1 = AgentResult(content="a")
    r2 = AgentResult(content="b")
    r1.metadata["key"] = "value"
    assert "key" not in r2.metadata


# --- Wave 4R additional tests (8) -------------------------------------------

def test_wave4r_agent_context_metadata_stored():
    """AgentContext with custom metadata dict stores those values."""
    from hive.agents.base import AgentContext
    ctx = AgentContext(metadata={"session_id": "abc-123", "user": "kamil"})
    assert ctx.metadata["session_id"] == "abc-123"
    assert ctx.metadata["user"] == "kamil"


def test_wave4r_agent_result_content_type_is_str():
    """AgentResult.content must be a str instance."""
    from hive.agents.base import AgentResult
    r = AgentResult(content="hello")
    assert isinstance(r.content, str)


def test_wave4r_delegate_named_unknown_raises_keyerror():
    """delegate_named raises KeyError for an unknown agent name."""
    import asyncio
    from hive.agents.delegate import delegate_named
    try:
        asyncio.run(delegate_named(["t"], "wave4r-no-such-agent"))
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_wave4r_terminal_outcome_has_exactly_4_members():
    """TerminalOutcome enum has exactly 4 members."""
    from hive.agents.base import TerminalOutcome
    assert len(list(TerminalOutcome)) == 4


def test_wave4r_agent_registry_contains_all_specialists(tmp_path):
    """agents_registry after HiveOS.build() contains all 5 expected specialist names."""
    hive = _make_hive(tmp_path)
    for name in _EXPECTED_AGENTS:
        assert name in hive.agents_registry


def test_wave4r_agent_context_tools_default_empty():
    """AgentContext.tools defaults to an empty list with no arguments."""
    from hive.agents.base import AgentContext
    ctx = AgentContext()
    assert ctx.tools == []
    assert isinstance(ctx.tools, list)


def test_wave4r_agent_result_outcome_default_completed():
    """AgentResult.outcome defaults to TerminalOutcome.COMPLETED."""
    from hive.agents.base import AgentResult, TerminalOutcome
    r = AgentResult(content="x")
    assert r.outcome == TerminalOutcome.COMPLETED


def test_wave4r_base_agent_run_is_async():
    """BaseAgent.run() is a coroutine function (async def)."""
    import inspect
    from hive.agents.base import BaseAgent
    assert inspect.iscoroutinefunction(BaseAgent.run)
