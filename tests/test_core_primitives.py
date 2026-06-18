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


def test_eventbus_history_count_and_clear():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {})
    bus.publish(EventType.INFERENCE_END, {})
    assert bus.history_count() == 2
    cleared = bus.clear_history()
    assert cleared == 2
    assert bus.history_count() == 0


def test_eventbus_unsubscribe_all():
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.APPROVAL_REQUESTED, lambda e: calls.append(1))
    bus.publish(EventType.APPROVAL_REQUESTED, {})
    assert calls == [1]
    removed = bus.unsubscribe_all(EventType.APPROVAL_REQUESTED)
    assert removed == 1
    bus.publish(EventType.APPROVAL_REQUESTED, {})
    assert calls == [1]  # no new calls after unsubscribe


def test_eventbus_subscribe_once():
    bus = EventBus()
    calls = []
    bus.subscribe_once(EventType.BUDGET_BLOCK, lambda e: calls.append(e.data.get("reason")))
    bus.publish(EventType.BUDGET_BLOCK, {"reason": "cap"})
    bus.publish(EventType.BUDGET_BLOCK, {"reason": "window"})  # second fire must not call
    assert calls == ["cap"]  # fired exactly once


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


def test_tool_result_bool():
    from hive.core.types import ToolResult
    ok = ToolResult(tool_name="t", content="hi", success=True)
    fail = ToolResult(tool_name="t", content="err", success=False)
    assert bool(ok) is True
    assert bool(fail) is False


def test_config_validate_default_secret(tmp_path):
    cfg = HiveConfig(
        root=tmp_path, data_dir=tmp_path, state_db=tmp_path / "s.db",
        exec_provider="minimax", minimax_anthropic_base="x", minimax_openai_base="x",
        minimax_api_key="", anthropic_base="x", anthropic_api_key="",
        exec_model="MiniMax-M3", exec_fallback_model="MiniMax-M2.7", aux_model="MiniMax-M2.7",
        planner_cmd="codex", planner_enabled=False, planner_timeout=120.0,
        remains_url="", daily_call_cap=3000, window_warn_pct=70.0,
        host="0.0.0.0", port=8088, secret="change_me",
        mnemosyne_mcp_url="", mnemosyne_home=tmp_path, obsidian_vault=tmp_path,
        heartbeat_sec=900, max_concurrent_agents=3,
        github_token="", github_repo="", github_owner="",
        telegram_token="", telegram_webhook_secret="",
        sandbox_image="", mcp_servers=(), max_iterations=30, max_per_tool=50,
        selfmod_failure_threshold=3, tool_timeout=60.0,
        shell_provider="local", shell_docker_image="alpine:latest",
        cors_origins="*", max_message_len=32000, ws_idle_timeout=300.0,
    )
    issues = cfg.validate()
    assert any("change_me" in i for i in issues)


def _base_cfg(tmp_path=None):
    from pathlib import Path
    p = tmp_path or Path("/tmp/hive_test_cfg")
    return dict(
        root=p, data_dir=p, state_db=p / "s.db",
        exec_provider="minimax", minimax_anthropic_base="x", minimax_openai_base="x",
        minimax_api_key="key", anthropic_base="x", anthropic_api_key="",
        exec_model="MiniMax-M3", exec_fallback_model="MiniMax-M2.7", aux_model="MiniMax-M2.7",
        planner_cmd="codex", planner_enabled=False, planner_timeout=120.0,
        remains_url="", daily_call_cap=3000, window_warn_pct=70.0,
        host="0.0.0.0", port=8088, secret="s3cr3t",
        mnemosyne_mcp_url="", mnemosyne_home=p, obsidian_vault=p,
        heartbeat_sec=900, max_concurrent_agents=3,
        github_token="", github_repo="", github_owner="",
        telegram_token="", telegram_webhook_secret="",
        sandbox_image="", mcp_servers=(), max_iterations=30, max_per_tool=50,
        selfmod_failure_threshold=3, tool_timeout=60.0,
        shell_provider="local", shell_docker_image="alpine:latest",
        cors_origins="*", max_message_len=32000, ws_idle_timeout=300.0,
    )


def test_config_validate_bad_exec_provider():
    cfg = HiveConfig(
        **{**_base_cfg(), "exec_provider": "unknown_provider"},
    )
    issues = cfg.validate()
    assert any("HIVE_EXEC_PROVIDER" in i for i in issues)


def test_config_validate_bad_shell_provider():
    cfg = HiveConfig(
        **{**_base_cfg(), "shell_provider": "ssh"},
    )
    issues = cfg.validate()
    assert any("HIVE_SHELL_PROVIDER" in i for i in issues)


def test_config_validate_zero_max_iterations():
    cfg = HiveConfig(
        **{**_base_cfg(), "max_iterations": 0},
    )
    issues = cfg.validate()
    assert any("HIVE_MAX_ITERATIONS" in i for i in issues)


def test_config_validate_minimax_no_key():
    cfg = HiveConfig(
        **{**_base_cfg(), "exec_provider": "minimax", "minimax_api_key": ""},
    )
    issues = cfg.validate()
    assert any("MINIMAX_API_KEY" in i for i in issues)


def test_eventbus_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count(EventType.INFERENCE_END) == 0
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    assert bus.subscriber_count(EventType.INFERENCE_END) == 2
    assert bus.subscriber_count(EventType.TOOL_CALL_END) == 0


def test_eventbus_total_subscribers():
    bus = EventBus()
    assert bus.total_subscribers() == 0
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_END, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_END, lambda e: None)
    assert bus.total_subscribers() == 3


def test_eventbus_history_by_type():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {"model": "m"})
    bus.publish(EventType.INFERENCE_END, {})
    bus.publish(EventType.TOOL_CALL_END, {})
    by_type = bus.history_by_type()
    assert by_type.get("inference_end") == 2
    assert by_type.get("tool_call_end") == 1
    assert "budget_block" not in by_type


def test_eventbus_history_by_type_empty():
    bus = EventBus(record_history=True)
    assert bus.history_by_type() == {}


def test_eventbus_recent_events_empty():
    bus = EventBus(record_history=True)
    assert bus.recent_events() == []


def test_eventbus_recent_events_returns_newest_first():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"a": 1})
    bus.publish(EventType.TOOL_CALL_END, {"b": 2})
    events = bus.recent_events(n=10)
    assert len(events) == 2
    assert events[0]["event_type"] == EventType.TOOL_CALL_END.value  # newest first
    assert events[1]["event_type"] == EventType.TOOL_CALL_START.value


def test_eventbus_recent_events_capped_by_n():
    bus = EventBus(record_history=True)
    for _ in range(10):
        bus.publish(EventType.TOOL_CALL_START, {})
    assert len(bus.recent_events(n=3)) == 3


def test_eventbus_recent_events_serialisable_fields():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"tool": "shell", "args": {}})
    ev = bus.recent_events(n=1)[0]
    assert "event_type" in ev and "data" in ev and "ts" in ev


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


# --- llm_summary / is_production / to_safe_dict ----------------------------

def test_hiveconfig_llm_summary(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    summary = cfg.llm_summary()
    assert "exec_model" in summary
    assert "exec_provider" in summary
    assert "planner_enabled" in summary
    assert "daily_call_cap" in summary
    assert isinstance(summary["planner_enabled"], bool)


def test_hiveconfig_is_production_false_for_default(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Default secret is "change_me" → not production
    assert cfg.is_production() is False


def test_hiveconfig_is_production_true_for_real_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET", "super-secure-secret-abc")
    monkeypatch.setenv("HIVE_HOST", "10.0.0.1")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.is_production() is True


def test_hiveconfig_to_safe_dict_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "secret-key-xyz")
    monkeypatch.setenv("HIVE_GITHUB_TOKEN", "ghp_faketoken")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    safe = cfg.to_safe_dict()
    assert safe["secret"] == "***"
    assert safe["minimax_api_key"] == "***"
    assert safe["github_token"] == "***"
    # Non-secret fields are not redacted
    assert safe["exec_model"] == cfg.exec_model
    assert safe["port"] == cfg.port


def test_hiveconfig_to_safe_dict_empty_secrets_not_redacted(tmp_path):
    # When keys are empty strings, they should show "" not "***"
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    safe = cfg.to_safe_dict()
    # minimax_api_key is "" by default — should be "" not "***"
    assert safe["minimax_api_key"] == ""


def test_config_validate_zero_max_message_len():
    cfg = HiveConfig(**{**_base_cfg(), "max_message_len": 0})
    issues = cfg.validate()
    assert any("HIVE_MAX_MESSAGE_LEN" in i for i in issues)


def test_config_validate_zero_ws_idle_timeout():
    cfg = HiveConfig(**{**_base_cfg(), "ws_idle_timeout": 0.0})
    issues = cfg.validate()
    assert any("HIVE_WS_IDLE_TIMEOUT" in i for i in issues)
