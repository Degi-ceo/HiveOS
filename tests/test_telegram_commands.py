"""Contract tests for deterministic Telegram commands."""
from __future__ import annotations

from types import SimpleNamespace

from hive.autonomy.tasks import TaskBoard
from hive.context.session_store import SessionStore
from hive.core.approval_store import ApprovalStore
from hive.gateway.telegram_commands import TelegramCommandService, parse_command
from hive.gateway.telegram_sessions import TelegramSessionBindings


class _Memory:
    name = "test-memory"

    def count(self):
        return {"facts": 3, "other": 2}


def _service(tmp_path):
    path = tmp_path / "state.sqlite"
    hive = SimpleNamespace(
        session_store=SessionStore(path),
        task_board=TaskBoard(path),
        approval_store=ApprovalStore(path),
        memory=_Memory(),
    )
    return hive, TelegramCommandService(hive, TelegramSessionBindings(path))


def _dispatch(service, text: str):
    command = parse_command(text)
    assert command is not None
    return service.dispatch(
        command, chat_id="42", user_id="7", thread_id="",
        legacy_session_id="telegram:42:7", is_owner=True,
    )


def test_parser_accepts_telegram_bot_suffix_and_rejects_non_commands():
    parsed = parse_command("/new@HiveBot project alpha")
    assert parsed is not None
    assert parsed.name == "new" and parsed.args == ("project", "alpha")
    assert parse_command("/not-a-command") is None
    assert parse_command("normal message") is None


def test_new_and_resume_preserve_prior_session_history(tmp_path):
    hive, service = _service(tmp_path)
    initial = _dispatch(service, "/status").session_id
    hive.session_store.append(initial, "user", "old history")

    fresh = _dispatch(service, "/new Project Alpha")
    assert fresh.session_id != initial
    assert "Previous history is preserved" in fresh.reply
    assert hive.session_store.get_title(fresh.session_id) == "Project Alpha"

    listed = _dispatch(service, "/sessions")
    assert "Project Alpha" in listed.reply and "1 messages" in listed.reply
    resumed = _dispatch(service, "/resume 2")
    assert resumed.session_id == initial
    assert hive.session_store.messages(initial)[0].content == "old history"


def test_read_only_commands_use_local_state_and_do_not_need_a_model(tmp_path):
    hive, service = _service(tmp_path)
    hive.task_board.enqueue("review", source="test")
    hive.approval_store.record_pending("approval-1", tool="deploy", args={"secret": "never-render"},
                                       reason="test", kind="tool")

    assert "Memory records: 5" in _dispatch(service, "/memory").reply
    assert "#1 pending — review" in _dispatch(service, "/tasks").reply
    approvals = _dispatch(service, "/approvals").reply
    assert "approval-1 — tool: deploy" in approvals
    assert "never-render" not in approvals
    assert "Unknown command" in _dispatch(service, "/does_not_exist").reply


def test_help_is_generated_from_the_central_registry(tmp_path):
    _, service = _service(tmp_path)
    reply = _dispatch(service, "/help resume").reply
    assert reply.startswith("/resume <number>")
    all_commands = _dispatch(service, "/commands").reply
    assert "/new [title]" in all_commands and "/approvals" in all_commands
