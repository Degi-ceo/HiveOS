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


# --- additional coverage -------------------------------------------------------

def test_agent_result_default_outcome_is_completed():
    """AgentResult without explicit outcome must default to COMPLETED."""
    r = AgentResult(content="work done")
    assert r.outcome is TerminalOutcome.COMPLETED


def test_agent_result_tool_results_populated():
    """AgentResult.tool_results must accept and preserve a populated list."""
    from hive.core.types import ToolResult
    tr1 = ToolResult(tool_name="shell", content="ls output")
    tr2 = ToolResult(tool_name="web_get", content="<html>")
    r = AgentResult(content="done", tool_results=[tr1, tr2])
    assert len(r.tool_results) == 2
    assert r.tool_results[0].tool_name == "shell"
    assert r.tool_results[1].tool_name == "web_get"


def test_terminal_outcome_has_max_turns_member():
    """TerminalOutcome.MAX_TURNS must exist and equal 'max_turns'."""
    assert TerminalOutcome.MAX_TURNS == "max_turns"


def test_terminal_outcome_has_loop_guard_member():
    """TerminalOutcome.LOOP_GUARD must exist and equal 'loop_guard'."""
    assert TerminalOutcome.LOOP_GUARD == "loop_guard"


def test_agent_result_metadata_preserved():
    """Metadata dict stored in AgentResult must be returned unchanged."""
    meta = {"session_id": "abc-123", "request_id": "xyz"}
    r = AgentResult(content="ok", metadata=meta)
    assert r.metadata["session_id"] == "abc-123"
    assert r.metadata["request_id"] == "xyz"


def test_base_agent_subclass_with_tool_using_agent():
    """A concrete ToolUsingAgent subclass can be instantiated and run."""
    class MyToolAgent(ToolUsingAgent):
        agent_id = "tool-agent"

        async def run(self, input, context=None, **kwargs):
            return AgentResult(content=f"tool-ran:{input}", turns=1)

    agent = MyToolAgent()
    assert agent.accepts_tools is True
    result = asyncio.run(agent.run("do something"))
    assert result.content == "tool-ran:do something"
    assert result.turns == 1


def test_agent_result_turns_stored():
    """turns field must reflect the value passed at construction."""
    r = AgentResult(content="x", turns=5)
    assert r.turns == 5


def test_agent_context_memory_results_independent():
    """memory_results on two separate AgentContext instances must not share state."""
    ctx1 = AgentContext()
    ctx2 = AgentContext()
    ctx1.memory_results.append("fact-1")
    assert ctx2.memory_results == []


# --- Wave 3K additional tests -------------------------------------------------

def test_terminal_outcome_tool_error_member():
    """TerminalOutcome has TOOL_ERROR member with value 'tool_error'."""
    assert TerminalOutcome.TOOL_ERROR == "tool_error"


def test_terminal_outcome_max_turns_is_string():
    """TerminalOutcome.MAX_TURNS is a plain string value."""
    assert isinstance(TerminalOutcome.MAX_TURNS, str)
    assert TerminalOutcome.MAX_TURNS == "max_turns"


def test_agent_result_outcome_tool_error():
    """AgentResult can store TOOL_ERROR outcome."""
    r = AgentResult(content="error", outcome=TerminalOutcome.TOOL_ERROR)
    assert r.outcome == TerminalOutcome.TOOL_ERROR
    assert r.outcome == "tool_error"


def test_agent_context_conversation_defaults_empty():
    """A fresh AgentContext has a Conversation with no messages."""
    from hive.core.types import Conversation
    ctx = AgentContext()
    assert isinstance(ctx.conversation, Conversation)
    assert len(ctx.conversation.messages) == 0


def test_agent_context_tools_defaults_empty():
    """A fresh AgentContext has an empty tools list."""
    ctx = AgentContext()
    assert ctx.tools == []


def test_agent_result_metadata_is_dict():
    """AgentResult.metadata defaults to an empty dict."""
    r = AgentResult(content="x")
    assert isinstance(r.metadata, dict)


# --- Wave 3O additional tests ---------------------------------------------------

def test_terminal_outcome_completed_value():
    """TerminalOutcome.COMPLETED string value is 'completed'."""
    assert TerminalOutcome.COMPLETED.value == "completed"


def test_terminal_outcome_loop_guard_value():
    """TerminalOutcome.LOOP_GUARD string value is 'loop_guard'."""
    assert TerminalOutcome.LOOP_GUARD.value == "loop_guard"


def test_agent_result_content_empty_string():
    """AgentResult accepts an empty string as content."""
    r = AgentResult(content="")
    assert r.content == ""
    assert r.outcome is TerminalOutcome.COMPLETED


def test_base_agent_agent_id_is_base():
    """BaseAgent.agent_id class attribute is 'base'."""
    assert BaseAgent.agent_id == "base"


def test_tool_using_agent_subclass_inherits_accepts_tools():
    """A subclass of ToolUsingAgent inherits accepts_tools=True."""
    class MyTool(ToolUsingAgent):
        agent_id = "mytool"
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="ok")

    assert MyTool.accepts_tools is True


def test_agent_context_memory_results_default():
    """AgentContext.memory_results defaults to an empty list."""
    ctx = AgentContext()
    assert ctx.memory_results == []


# --- Wave 3Q additional tests ---------------------------------------------------

def test_terminal_outcome_tool_error_string_value():
    """TerminalOutcome.TOOL_ERROR == 'tool_error' as a plain string."""
    assert TerminalOutcome.TOOL_ERROR == "tool_error"
    assert TerminalOutcome.TOOL_ERROR.value == "tool_error"


def test_agent_result_loop_guard_outcome():
    """AgentResult stores LOOP_GUARD outcome correctly."""
    r = AgentResult(content="stopped", outcome=TerminalOutcome.LOOP_GUARD)
    assert r.outcome is TerminalOutcome.LOOP_GUARD
    assert r.outcome == "loop_guard"


def test_agent_result_max_turns_outcome():
    """AgentResult stores MAX_TURNS outcome correctly."""
    r = AgentResult(content="exhausted", outcome=TerminalOutcome.MAX_TURNS)
    assert r.outcome is TerminalOutcome.MAX_TURNS


def test_agent_context_can_hold_conversation_messages():
    """AgentContext.conversation accepts messages added after construction."""
    from hive.core.types import Message, Role
    ctx = AgentContext()
    ctx.conversation.messages.append(Message(role=Role.USER, content="hi"))
    assert len(ctx.conversation.messages) == 1
    assert ctx.conversation.messages[0].content == "hi"


def test_base_agent_accepts_tools_false():
    """BaseAgent.accepts_tools class attribute is False."""
    assert BaseAgent.accepts_tools is False


def test_agent_result_content_unicode():
    """AgentResult preserves unicode content."""
    r = AgentResult(content="héllo wörld 🌍")
    assert r.content == "héllo wörld 🌍"


# --- Wave 3W additional tests ---------------------------------------------------

def test_wave3w_agent_context_tools_independence():
    """tools list is independent between two AgentContext instances."""
    ctx1 = AgentContext()
    ctx2 = AgentContext()
    ctx1.tools.append("search")
    assert ctx2.tools == []


def test_wave3w_agent_result_explicit_zero_turns():
    """AgentResult with turns=0 explicitly set stores zero correctly."""
    r = AgentResult(content="done", turns=0)
    assert r.turns == 0


def test_wave3w_terminal_outcome_values_in_string_set():
    """All TerminalOutcome values are members of the expected string set."""
    expected = {"completed", "max_turns", "loop_guard", "tool_error"}
    actual = {o.value for o in TerminalOutcome}
    assert actual == expected


def test_wave3w_terminal_outcome_not_equal_to_arbitrary_string():
    """TerminalOutcome members do not compare equal to unrelated strings."""
    assert TerminalOutcome.COMPLETED != "done"
    assert TerminalOutcome.MAX_TURNS != "timeout"
    assert TerminalOutcome.LOOP_GUARD != "cycle"
    assert TerminalOutcome.TOOL_ERROR != "error"


def test_wave3w_agent_context_conversations_independent():
    """Two AgentContext instances have separate Conversation objects."""
    from hive.core.types import Message, Role
    ctx1 = AgentContext()
    ctx2 = AgentContext()
    ctx1.conversation.messages.append(Message(role=Role.USER, content="ping"))
    assert len(ctx2.conversation.messages) == 0


def test_wave3w_base_agent_subclass_agent_id_does_not_affect_parent():
    """Overriding agent_id on a subclass leaves BaseAgent.agent_id unchanged."""
    class SubAgent(BaseAgent):
        agent_id = "custom"
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="ok")

    assert SubAgent.agent_id == "custom"
    assert BaseAgent.agent_id == "base"


def test_wave3w_agent_result_tool_results_order_preserved():
    """AgentResult preserves the order of tool_results as given."""
    from hive.core.types import ToolResult
    tr1 = ToolResult(tool_name="a", content="first")
    tr2 = ToolResult(tool_name="b", content="second")
    tr3 = ToolResult(tool_name="c", content="third")
    r = AgentResult(content="x", tool_results=[tr1, tr2, tr3])
    assert [t.tool_name for t in r.tool_results] == ["a", "b", "c"]


def test_wave3w_tool_using_agent_receives_context():
    """ToolUsingAgent subclass can receive and use an AgentContext."""
    class CtxAgent(ToolUsingAgent):
        agent_id = "ctx-agent"
        async def run(self, input, context=None, **kwargs):
            tools_count = len(context.tools) if context else -1
            return AgentResult(content=str(tools_count))

    agent = CtxAgent()
    ctx = AgentContext(tools=["shell", "web_get"])
    result = asyncio.run(agent.run("task", context=ctx))
    assert result.content == "2"


# --- Wave 4A-A new tests -------------------------------------------------------

def test_wave4a_agent_result_tool_results_not_none_by_default():
    """AgentResult.tool_results defaults to an empty list, not None."""
    r = AgentResult(content="x")
    assert r.tool_results is not None
    assert r.tool_results == []


def test_wave4a_agent_context_conversation_max_messages_default():
    """AgentContext.conversation has max_messages=None by default."""
    ctx = AgentContext()
    assert ctx.conversation.max_messages is None


def test_wave4a_agent_result_large_turns_value():
    """AgentResult.turns accepts and stores a large integer correctly."""
    r = AgentResult(content="many", turns=10_000)
    assert r.turns == 10_000


def test_wave4a_agent_result_multiple_tool_results_same_tool_name():
    """AgentResult.tool_results can hold multiple results for the same tool_name."""
    from hive.core.types import ToolResult
    tr1 = ToolResult(tool_name="shell", content="first")
    tr2 = ToolResult(tool_name="shell", content="second")
    r = AgentResult(content="done", tool_results=[tr1, tr2])
    names = [t.tool_name for t in r.tool_results]
    assert names == ["shell", "shell"]


def test_wave4a_agent_context_metadata_mutation():
    """Mutating AgentContext.metadata after construction persists on the same instance."""
    ctx = AgentContext()
    ctx.metadata["key"] = "value"
    assert ctx.metadata["key"] == "value"
    ctx.metadata["key"] = "updated"
    assert ctx.metadata["key"] == "updated"


def test_wave4a_terminal_outcome_member_count():
    """TerminalOutcome has exactly four members."""
    assert len(list(TerminalOutcome)) == 4


def test_wave4a_base_agent_run_is_abstract():
    """BaseAgent cannot be instantiated because run() is abstract."""
    with pytest.raises(TypeError):
        BaseAgent()  # type: ignore[abstract]


def test_wave4a_agent_context_with_memory_results():
    """AgentContext accepts and stores a pre-populated memory_results list."""
    ctx = AgentContext(memory_results=["fact-a", "fact-b"])
    assert ctx.memory_results == ["fact-a", "fact-b"]
