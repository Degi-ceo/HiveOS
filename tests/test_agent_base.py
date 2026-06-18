"""agents/base.py — AgentContext, AgentResult, TerminalOutcome, BaseAgent protocol."""
from __future__ import annotations

import asyncio

import pytest

from hive.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    TerminalOutcome,
    ToolUsingAgent,
)
from hive.core.types import Conversation


# --- TerminalOutcome -----------------------------------------------------------

def test_terminal_outcome_values():
    assert TerminalOutcome.COMPLETED == "completed"
    assert TerminalOutcome.MAX_TURNS == "max_turns"
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"
    assert TerminalOutcome.TOOL_ERROR == "tool_error"


def test_terminal_outcome_is_str_subclass():
    assert isinstance(TerminalOutcome.COMPLETED, str)
    assert isinstance(TerminalOutcome.MAX_TURNS, str)


def test_terminal_outcome_str_equality():
    assert TerminalOutcome.COMPLETED == "completed"
    assert TerminalOutcome.MAX_TURNS == "max_turns"
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"
    assert TerminalOutcome.TOOL_ERROR == "tool_error"


def test_terminal_outcome_all_members_distinct():
    values = [o.value for o in TerminalOutcome]
    assert len(values) == len(set(values))


# --- AgentResult ---------------------------------------------------------------

def test_agent_result_default_outcome_is_completed():
    r = AgentResult(content="hello")
    assert r.outcome is TerminalOutcome.COMPLETED


def test_agent_result_default_fields():
    r = AgentResult(content="hi")
    assert r.content == "hi"
    assert r.tool_results == []
    assert r.turns == 0
    assert r.metadata == {}


def test_agent_result_all_fields_specified():
    from hive.core.types import ToolResult
    tr = ToolResult(tool_name="shell", content="ok")
    r = AgentResult(
        content="done",
        tool_results=[tr],
        turns=3,
        metadata={"key": "val"},
        outcome=TerminalOutcome.MAX_TURNS,
    )
    assert r.content == "done"
    assert r.tool_results == [tr]
    assert r.turns == 3
    assert r.metadata == {"key": "val"}
    assert r.outcome is TerminalOutcome.MAX_TURNS


def test_agent_result_outcome_set_to_each_value():
    for outcome in TerminalOutcome:
        r = AgentResult(content="x", outcome=outcome)
        assert r.outcome is outcome


# --- AgentContext --------------------------------------------------------------

def test_agent_context_defaults():
    ctx = AgentContext()
    assert isinstance(ctx.conversation, Conversation)
    assert ctx.tools == []
    assert ctx.memory_results == []
    assert ctx.metadata == {}


def test_agent_context_with_tools():
    ctx = AgentContext(tools=["shell", "search"])
    assert ctx.tools == ["shell", "search"]


def test_agent_context_metadata_is_independent():
    ctx1 = AgentContext()
    ctx2 = AgentContext()
    ctx1.metadata["x"] = 1
    assert "x" not in ctx2.metadata


# --- BaseAgent / ToolUsingAgent protocol --------------------------------------

def test_base_agent_is_abstract():
    with pytest.raises(TypeError):
        BaseAgent()  # type: ignore[abstract]


def test_base_agent_class_attributes():
    assert BaseAgent.agent_id == "base"
    assert BaseAgent.accepts_tools is False


def test_tool_using_agent_accepts_tools_true():
    assert ToolUsingAgent.accepts_tools is True


def test_concrete_agent_can_be_instantiated():
    class MyAgent(BaseAgent):
        agent_id = "my"

        async def run(self, input, context=None, **kwargs):
            return AgentResult(content=f"ran:{input}")

    agent = MyAgent()
    result = asyncio.run(agent.run("task"))
    assert result.content == "ran:task"
    assert result.outcome is TerminalOutcome.COMPLETED
