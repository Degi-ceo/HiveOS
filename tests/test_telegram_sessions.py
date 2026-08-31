"""Regression tests for durable Telegram conversation session selection."""
from __future__ import annotations

from hive.gateway.telegram_sessions import TelegramSessionBindings


def _legacy(chat: str = "42", user: str = "7") -> str:
    return f"telegram:{chat}:{user}"


def test_first_use_preserves_legacy_session_and_marks_it_active(tmp_path):
    store = TelegramSessionBindings(tmp_path / "state.sqlite")

    session_id = store.active_or_create(
        chat_id="42", user_id="7", thread_id="", legacy_session_id=_legacy()
    )

    assert session_id == _legacy()
    sessions = store.sessions(chat_id="42", user_id="7", thread_id="")
    assert [(item.session_id, item.active) for item in sessions] == [(_legacy(), True)]


def test_new_session_keeps_history_and_resume_selects_it_without_deleting(tmp_path):
    store = TelegramSessionBindings(tmp_path / "state.sqlite")
    original = store.active_or_create(
        chat_id="42", user_id="7", thread_id="", legacy_session_id=_legacy()
    )
    fresh = store.new_session(
        chat_id="42", user_id="7", thread_id="", legacy_session_id=_legacy()
    )

    sessions = store.sessions(chat_id="42", user_id="7", thread_id="")
    assert sessions[0].session_id == fresh and sessions[0].active is True
    assert {item.session_id for item in sessions} == {original, fresh}
    assert store.resume(chat_id="42", user_id="7", thread_id="", index=2) == original
    resumed = store.sessions(chat_id="42", user_id="7", thread_id="")
    assert resumed[0].session_id == original and resumed[0].active is True


def test_session_bindings_are_isolated_by_user_and_survive_reopen(tmp_path):
    path = tmp_path / "state.sqlite"
    store = TelegramSessionBindings(path)
    first = store.active_or_create(
        chat_id="42", user_id="7", thread_id="", legacy_session_id=_legacy()
    )
    second = store.active_or_create(
        chat_id="42", user_id="8", thread_id="", legacy_session_id=_legacy("42", "8")
    )
    store.close()

    reopened = TelegramSessionBindings(path)
    assert reopened.active_or_create(
        chat_id="42", user_id="7", thread_id="", legacy_session_id="ignored"
    ) == first
    assert reopened.active_or_create(
        chat_id="42", user_id="8", thread_id="", legacy_session_id="ignored"
    ) == second
    assert reopened.resume(chat_id="42", user_id="7", thread_id="", index=3) is None