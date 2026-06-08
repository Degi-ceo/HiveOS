"""Tests for the P1 core primitives + PROTECTED-file bridges."""
from __future__ import annotations

import pytest

from hiveos.core.registry import RegistryBase
from hiveos.core.events import EventBus, EventType
from hiveos.core.types import Message, Role, Conversation, ToolCall


def test_registry_isolation_and_ops():
    class A(RegistryBase[str]):
        pass

    class B(RegistryBase[str]):
        pass

    A.register_value("x", "a-value")
    assert A.contains("x") and not B.contains("x")  # isolation
    assert A.get("x") == "a-value"
    with pytest.raises(ValueError):
        A.register_value("x", "dup")  # duplicate detection
    assert list(A.keys()) == ["x"]
    A.clear()
    assert not A.contains("x")


def test_registry_decorator_and_create():
    class Reg(RegistryBase[type]):
        pass

    @Reg.register("thing")
    class Thing:
        def __init__(self, n: int) -> None:
            self.n = n

    obj = Reg.create("thing", 5)
    assert obj.n == 5


def test_eventbus_pubsub_and_isolation():
    bus = EventBus(record_history=True)
    seen: list[str] = []
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: seen.append(e.data.get("tool", "")))

    def boom(_e):
        raise RuntimeError("bad subscriber")

    bus.subscribe(EventType.TOOL_CALL_START, boom)  # must not break others
    bus.publish(EventType.TOOL_CALL_START, {"tool": "read_file"})
    bus.publish(EventType.INFERENCE_START, {})  # different type, ignored by sub

    assert seen == ["read_file"]
    assert len(bus.history()) == 2


def test_message_to_dict_roundtrip():
    m = Message(role=Role.ASSISTANT, content="hi",
                tool_calls=[ToolCall(id="c1", name="t", arguments="{}")])
    d = m.to_dict()
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "t"


def test_conversation_sliding_window():
    c = Conversation(max_messages=2)
    for i in range(4):
        c.add(Message(role=Role.USER, content=str(i)))
    assert [m.content for m in c.messages] == ["2", "3"]
