"""M3 terminal sessions — explicit cross-channel continuity without raw IDs."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.gateway.channels.base import MessageEvent, SendResult
from hive.gateway.channels.telegram import TelegramChannel
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _Router:
    async def complete(self, *_args, **_kwargs):
        return CompletionResult(text="ok", model="test")

    async def aclose(self):
        return None


def test_runtime_channel_session_link_is_opt_in_and_cross_surface(tmp_path):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="session-link-test-secret",
        production_mode=False,
        autonomy_enabled=False,
        host="127.0.0.1",
    )
    hive = HiveOS.build(cfg, router=_Router())
    try:
        # Existing deployments retain their current IDs until explicitly linked.
        assert hive.resolve_channel_session(
            "telegram", "tg-user-1", legacy_session_id="telegram:chat-1",
        ) == "telegram:chat-1"

        hive.link_channel_session("telegram", "tg-user-1", "owner-work")
        hive.link_channel_session("slack", "slack-user-1", "owner-work")
        assert hive.resolve_channel_session(
            "telegram", "tg-user-1", legacy_session_id="telegram:chat-1",
        ) == "owner-work"
        assert hive.resolve_channel_session(
            "slack", "slack-user-1", legacy_session_id="slack:channel-9",
        ) == "owner-work"
        assert hive.session_store.linked_surfaces("owner-work") == {"slack": 1, "telegram": 1}
        assert b"tg-user-1" not in cfg.state_db.read_bytes()
        assert b"slack-user-1" not in cfg.state_db.read_bytes()
    finally:
        asyncio.run(hive.aclose())


def test_telegram_webhook_uses_explicitly_linked_conversation(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="session-link-test-secret",
        production_mode=False,
        autonomy_enabled=False,
        host="127.0.0.1",
        telegram_token="telegram-token",
        telegram_webhook_secret="webhook-secret",
        telegram_allowed_user_ids=frozenset({"operator-1"}),
    )
    hive = HiveOS.build(cfg, router=_Router())
    hive.link_channel_session("telegram", "operator-1", "owner-work")
    ask = AsyncMock(return_value="linked reply")
    monkeypatch.setattr(type(hive), "ask", ask)
    telegram = MagicMock(spec=TelegramChannel)
    telegram.parse_update.return_value = MessageEvent(
        text="continue work", chat_id="chat-9", user_id="operator-1",
        message_id="message-1", platform="telegram",
    )
    telegram.send = AsyncMock(return_value=SendResult(ok=True, message_id="out-1"))

    try:
        with TestClient(create_app(hive, telegram=telegram)) as client:
            response = client.post("/telegram/webhook", json={"message": {}}, headers={
                "X-Telegram-Bot-Api-Secret-Token": "webhook-secret",
            })
        assert response.status_code == 200
        ask.assert_awaited_once_with("continue work", session_id="owner-work", channel_hint="telegram")
    finally:
        asyncio.run(hive.aclose())


def test_operator_stream_is_correlated_and_hides_internal_tool_payloads(tmp_path, monkeypatch):
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="session-link-test-secret",
        production_mode=False,
        autonomy_enabled=False,
        host="127.0.0.1",
    )
    monkeypatch.setenv("HIVE_TEST_SECRET", "do-not-show")
    hive = HiveOS.build(cfg, router=_Router())

    async def raw_events(*_args, **_kwargs):
        yield {
            "type": "model_decision", "turn": 1, "text": "private reasoning do-not-show",
            "tool_calls": [{"id": "call-1", "name": "shell", "arguments": "do-not-show"}],
        }
        yield {
            "type": "tool_call_start", "turn": 1, "id": "call-1", "name": "shell",
            "arguments": "do-not-show",
        }
        yield {
            "type": "tool_call_end", "turn": 1, "id": "call-1", "name": "shell",
            "status": "ok", "content": "raw tool output do-not-show",
        }
        yield {"type": "final", "turn": 1, "text": "answer do-not-show", "tool_calls": 1}

    hive.orchestrator.stream_ask = raw_events
    try:
        events = asyncio.run(_collect(hive.stream_ask_iterations("continue", session_id="owner-work")))
        persisted = hive.run_ledger.events(events[0]["run_id"])
    finally:
        asyncio.run(hive.aclose())

    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert len({event["run_id"] for event in events}) == 1
    assert {event["session_id"] for event in events} == {"owner-work"}
    wire = str(events)
    assert "arguments" not in wire and "raw tool output" not in wire and "private reasoning" not in wire
    assert "do-not-show" not in wire
    assert events[-1]["text"] == "answer ***REDACTED***"
    assert events[2]["duration_ms"] >= 0
    assert events[2]["summary"] == "completed"
    durable_wire = str(persisted)
    assert [row["type"] for row in persisted] == [
        "operator.model_decision", "operator.tool_call_start", "operator.tool_call_end", "operator.final",
    ]
    assert "arguments" not in durable_wire and "raw tool output" not in durable_wire
    assert "private reasoning" not in durable_wire and "do-not-show" not in durable_wire


async def _collect(stream):
    return [event async for event in stream]


def test_specialist_delegation_creates_a_durable_child_run(tmp_path):
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import register_agent
    from hive.core.events import EventBus, EventType
    from hive.core.run_context import bind_run_id
    from hive.observability.runs import RunLedger
    from hive.tools.builtins import DelegateToSpecialist

    class _Leaf(BaseAgent):
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="review complete")

    bus = EventBus()
    ledger = RunLedger(tmp_path / "state.sqlite").attach(bus)
    ledger.begin("parent-run", kind="conversation", session_id="owner-work")
    register_agent("m3-observer", lambda: _Leaf())

    async def delegate_once():
        with bind_run_id("parent-run"):
            return await DelegateToSpecialist(bus=bus).execute(agent="m3-observer", task="review")

    try:
        assert asyncio.run(delegate_once()).content == "review complete"
        children = ledger.children("parent-run")
        assert len(children) == 1
        assert children[0]["kind"] == "subagent"
        assert children[0]["session_id"] == "owner-work"
        assert children[0]["state"] == "ok"
        parent_events = ledger.events("parent-run")
        assert [event["type"] for event in parent_events if event["type"].startswith("subagent_")] == [
            EventType.SUBAGENT_STARTED.value, EventType.SUBAGENT_COMPLETED.value,
        ]
    finally:
        ledger.close()


def test_local_shell_provider_runs_a_portable_real_subprocess():
    """One native Python child process works under the host shell on every CI OS."""
    from hive.tools.shell_provider import LocalShellProvider

    command = f'"{sys.executable}" -c "print(\'M3_PORTABLE_SHELL_OK\')"'
    result = asyncio.run(LocalShellProvider().run(command))

    assert result.returncode == 0
    assert result.stdout.strip() == "M3_PORTABLE_SHELL_OK"


def test_session_operator_views_redact_links_but_preserve_transcript(tmp_path):
    from hive.context.session_store import SessionStore, opaque_subject_id
    from hive.core.types import Role

    store = SessionStore(tmp_path / "state.sqlite")
    try:
        store.append("owner-work", Role.USER, "hello")
        store.append("owner-work", Role.ASSISTANT, "hi")
        key = opaque_subject_id("telegram", "private-chat-id", "test-secret")
        store.bind_link("telegram", key, "owner-work")
        messages = store.message_details("owner-work")
        links = store.link_details("owner-work")
    finally:
        store.close()

    assert [(row["role"], row["content"]) for row in messages] == [("user", "hello"), ("assistant", "hi")]
    assert links[0]["surface"] == "telegram"
    assert links[0]["ref"] == key[3:15]
    assert "private-chat-id" not in str(links)


def test_model_memory_write_stays_untrusted_and_capped(tmp_path):
    from hive.core.types import ContentTrust
    from hive.memory.local import LocalMemoryProvider
    from hive.tools.builtins import RememberMemory

    memory = LocalMemoryProvider(tmp_path / "state.sqlite")
    try:
        result = asyncio.run(RememberMemory(memory).execute(
            content="agent observation", topic="m4-test", importance=0.99,
        ))
        rows = memory.recall("m4-test", limit=5)
        trusted_rows = memory.recall("m4-test", limit=5, trusted_only=True)
    finally:
        memory.close()

    assert result.success
    assert rows[0]["trust"] == ContentTrust.UNTRUSTED.value
    assert rows[0]["importance"] == 0.5
    assert trusted_rows == []


def test_watch_replays_only_safe_durable_operator_events(tmp_path, monkeypatch, capsys):
    from hive.core.config import HiveConfig
    from hive.observability.runs import RunLedger
    from hive.surfaces import cli

    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), state_db=tmp_path / "state.sqlite")
    ledger = RunLedger(cfg.state_db)
    try:
        ledger.begin("run-m4-watch", kind="conversation", session_id="owner-work")
        ledger.record_operator_event({
            "version": 1, "type": "tool_call_end", "run_id": "run-m4-watch",
            "session_id": "owner-work", "sequence": 2, "timestamp": 1.0,
            "name": "shell", "status": "ok", "summary": "completed", "duration_ms": 7,
        })
        ledger.finish("run-m4-watch", state="ok")
    finally:
        ledger.close()
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))

    assert cli.main(["watch", "run-m4-watch"]) == 0
    output = capsys.readouterr().out
    assert "tool_call_end shell ok (7 ms) — completed" in output
    assert "owner-work" not in output


def test_owner_memory_command_is_trusted_but_rejects_configured_secrets(tmp_path, monkeypatch, capsys):
    from hive.core.types import ContentTrust
    from hive.memory.local import LocalMemoryProvider
    from hive.runtime import HiveOS
    from hive.surfaces import cli

    memory = LocalMemoryProvider(tmp_path / "state.sqlite")

    class _Hive:
        def __init__(self):
            self.memory = memory

        async def aclose(self):
            return None

    monkeypatch.setattr(HiveOS, "build", classmethod(lambda cls, **_kwargs: _Hive()))
    monkeypatch.setenv("HIVE_TEST_SECRET", "m4-keep-out")
    try:
        assert asyncio.run(cli._memory_remember("owner preference", topic="m4-owner")) == 0
        assert asyncio.run(cli._memory_remember("m4-keep-out", topic="secret")) == 2
        rows = memory.recall("m4-owner", trusted_only=True)
    finally:
        memory.close()

    assert rows[0]["trust"] == ContentTrust.TRUSTED.value
    assert "Stored trusted memory" in capsys.readouterr().out
