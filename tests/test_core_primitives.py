"""Tests for the P1 core primitives + PROTECTED-file bridges."""
from __future__ import annotations

import dataclasses

import pytest

from hive.core.registry import RegistryBase
from hive.core.events import EventBus, EventType
from hive.core.types import Message, Role, Conversation, ToolCall
from hive.core.config import HiveConfig


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


def test_hiveconfig_builds_from_env_and_is_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "MiniMax-M9")
    monkeypatch.setenv("HIVE_PORT", "9099")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "d"))
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.exec_model == "MiniMax-M9"
    assert cfg.port == 9099  # typed: int, not str
    assert cfg.state_db == tmp_path / "d" / "hive.sqlite"
    # frozen: impossible-state protection
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 1  # type: ignore[misc]
    # no import-time side effects: data dir is created only on explicit request
    assert not cfg.data_dir.exists()
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()
