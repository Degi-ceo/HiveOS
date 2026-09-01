"""Gateway-level contract tests for deterministic Telegram commands."""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.gateway.channels.base import MessageEvent, SendResult
from hive.gateway.channels.telegram import TelegramChannel
from hive.gateway.channels.telegram_inbox import TelegramInbox
from hive.gateway.telegram_sessions import TelegramSessionBindings
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kwargs):
        return CompletionResult(text="model reply", model="test")

    async def aclose(self):
        return None


def _hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = dataclasses.replace(
        cfg,
        telegram_token="test-token",
        telegram_webhook_secret="test_secret",
        telegram_allowed_user_ids=frozenset({"7"}),
        telegram_owner_user_ids=frozenset({"7"}),
    )
    return HiveOS.build(cfg, router=_Router())


def _telegram(event: MessageEvent):
    telegram = MagicMock(spec=TelegramChannel)
    telegram.parse_update.return_value = event
    telegram.send = AsyncMock(return_value=SendResult(ok=True, message_id="sent-1"))
    telegram.send_typing = AsyncMock(return_value=True)
    return telegram


def _post(client, update_id: int, text: str):
    return client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        json={"update_id": update_id, "message": {"text": text, "chat": {"id": 42}}},
    )


def test_new_command_is_deduplicated_and_never_calls_the_model(tmp_path):
    hive = _hive(tmp_path)
    event = MessageEvent(text="/new Project Alpha", chat_id="42", user_id="7", message_id="9", platform="telegram")
    telegram = _telegram(event)
    inbox = TelegramInbox(tmp_path / "inbox.sqlite")
    bindings = TelegramSessionBindings(tmp_path / "state.sqlite")
    app = create_app(hive, telegram=telegram, telegram_inbox=inbox, telegram_sessions=bindings)

    with patch.object(HiveOS, "ask", new=AsyncMock(return_value="must not run")) as ask:
        with TestClient(app) as client:
            first = _post(client, 100, "/new Project Alpha")
            duplicate = _post(client, 100, "/new Project Alpha")

    assert first.json() == {"ok": True, "handled": True}
    assert duplicate.json() == {"ok": True, "handled": True, "duplicate": True}
    assert ask.await_count == 0
    assert telegram.send_typing.await_count == 0
    assert telegram.send.await_count == 1
    assert "Previous history is preserved" in telegram.send.await_args.args[0].text
    reopened = TelegramSessionBindings(tmp_path / "state.sqlite")
    sessions = reopened.sessions(chat_id="42", user_id="7", thread_id="")
    assert len(sessions) == 2


def test_owner_correction_is_deduplicated_and_never_calls_the_model(tmp_path):
    hive = _hive(tmp_path)
    hive.memory.learn("fact", "owner", "Old owner", "seed")
    event = MessageEvent(
        text="/correct fact:owner | Kamil | Owner corrected this fact",
        chat_id="42", user_id="7", message_id="10", platform="telegram",
    )
    telegram = _telegram(event)
    app = create_app(
        hive,
        telegram=telegram,
        telegram_inbox=TelegramInbox(tmp_path / "inbox.sqlite"),
        telegram_sessions=TelegramSessionBindings(tmp_path / "state.sqlite"),
    )

    with patch.object(HiveOS, "ask", new=AsyncMock(return_value="must not run")) as ask:
        with TestClient(app) as client:
            first = _post(client, 102, event.text)
            duplicate = _post(client, 102, event.text)

    assert first.json() == {"ok": True, "handled": True}
    assert duplicate.json() == {"ok": True, "handled": True, "duplicate": True}
    assert ask.await_count == 0
    memory = hive.memory_ledger.recall_current("owner")[0]
    assert memory["content"] == "Kamil"
    assert memory["explanation"]["correction_of_version"] == 1
    assert "version 2" in telegram.send.await_args.args[0].text


def test_normal_message_keeps_model_path_and_uses_bound_session(tmp_path):
    hive = _hive(tmp_path)
    event = MessageEvent(text="hello", chat_id="42", user_id="7", message_id="9", platform="telegram")
    telegram = _telegram(event)
    app = create_app(
        hive,
        telegram=telegram,
        telegram_inbox=TelegramInbox(tmp_path / "inbox.sqlite"),
        telegram_sessions=TelegramSessionBindings(tmp_path / "state.sqlite"),
    )

    with patch.object(HiveOS, "ask", new=AsyncMock(return_value="model reply")) as ask:
        with TestClient(app) as client:
            response = _post(client, 101, "hello")

    assert response.json() == {"ok": True, "handled": True}
    assert ask.await_count == 1
    assert ask.await_args.kwargs["channel_hint"] == "telegram"
    assert ask.await_args.kwargs["session_id"] == "telegram:42:7"
    assert telegram.send_typing.await_count == 1
