"""M64 terminal input: session-scoped history and interactive editor behavior."""

from __future__ import annotations

import asyncio
import sys

from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput

from hive.context.session_store import SessionStore
from hive.core.types import Role
from hive.surfaces import cli
from hive.surfaces.cli.repl_input import ChatInput, _toolbar


def _force_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_history_is_opt_in_and_limited_to_named_session(monkeypatch):
    _force_tty(monkeypatch)
    store = SessionStore(":memory:")
    try:
        store.append("chosen", Role.USER, "first question")
        store.append("chosen", Role.ASSISTANT, "answer should not enter history")
        store._db.execute(
            "INSERT INTO messages(session, ts, role, content) VALUES(?,?,?,?)",
            ("chosen", 0.0, "unrecognized-tool-role", "opaque tool payload"),
        )
        store._db.commit()
        store.append("other", Role.USER, "other private question")
        store.append("chosen", Role.USER, "second question")
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            opted = ChatInput(
                session_store=store, session_id="chosen", model="model-v1",
                history_enabled=True,
            )
            default = ChatInput(session_store=store, session_id="chosen", model="model-v1")
        assert opted._prompt.history.get_strings() == ["first question", "second question"]
        assert default._prompt.history.get_strings() == []
        assert "history: 2 loaded" in opted._toolbar
        assert "history: off" in default._toolbar
        assert "question" not in opted._toolbar
    finally:
        store.close()


def test_toolbar_never_displays_raw_session_or_untrusted_model():
    result = _toolbar(
        model="private\x1b[31m key", session_id="telegram:private-chat-id",
        history_enabled=True, loaded=1,
    )
    assert "private-chat-id" not in result
    assert "private" not in result
    assert "\x1b" not in result
    assert "history: 1 loaded" in result
    secret_like = "ghp_" + "a" * 16
    assert secret_like not in _toolbar(
        model=secret_like, session_id="s", history_enabled=False, loaded=0,
    )
    assert "MiniMax" in _toolbar(
        model="MiniMax-M3", session_id="s", history_enabled=False, loaded=0,
    )
    assert "MiniMax-M3" not in _toolbar(
        model="MiniMax-M3", session_id="s", history_enabled=False, loaded=0,
    )


def test_history_seed_has_a_fixed_recent_message_limit(monkeypatch):
    _force_tty(monkeypatch)
    store = SessionStore(":memory:")
    try:
        for index in range(210):
            store.append("chosen", Role.USER, f"turn-{index}")
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            editor = ChatInput(
                session_store=store, session_id="chosen", model="m",
                history_enabled=True,
            )
        strings = editor._prompt.history.get_strings()
        assert len(strings) == 200
        assert strings[0] == "turn-10"
        assert strings[-1] == "turn-209"
    finally:
        store.close()


def test_slash_completion_only_for_command_prefix(monkeypatch):
    _force_tty(monkeypatch)
    with create_app_session(input=DummyInput(), output=DummyOutput()):
        editor = ChatInput(session_store=None, session_id="s", model="m")
    completer = editor._prompt.completer
    matches = list(completer.get_completions(Document("/st"), None))
    assert [match.text for match in matches] == ["/status"]
    assert list(completer.get_completions(Document("hello /st"), None)) == []
    assert list(completer.get_completions(Document("/status private"), None)) == []


def test_interactive_input_reads_from_terminal_pipe(monkeypatch):
    _force_tty(monkeypatch)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            editor = ChatInput(session_store=None, session_id="s", model="m")
            pipe.send_text("/help\r")
            assert asyncio.run(editor.read()) == "/help"


def test_redirected_input_uses_plain_fallback(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "/quit")
    editor = ChatInput(session_store=None, session_id="s", model="m", history_enabled=True)
    assert editor._prompt is None
    assert asyncio.run(editor.read()) == "/quit"


def test_chat_history_requires_explicit_session(monkeypatch, capsys):
    spec, parsed = cli._parser_mod.parse(["chat", "--session", "chosen", "--history"])
    assert spec.name == "chat" and parsed.history is True
    assert cli.main(["chat", "--history"]) == 2
    assert "--session" in capsys.readouterr().err

    received = {}

    async def fake_chat(session_id=None, *, history=False):
        received.update(session_id=session_id, history=history)
        return 0

    monkeypatch.setattr(cli, "_chat", fake_chat)
    assert cli.main(["chat", "--session", "chosen", "--history"]) == 0
    assert received == {"session_id": "chosen", "history": True}
    assert cli.main(["chat", "--session", "chosen", "--history", "--history"]) == 2
    assert cli.main(["chat", "--session", "--history"]) == 2
    assert cli.main(["chat", "--session", "-project"]) == 0
    assert received == {"session_id": "-project", "history": False}


def test_banner_reports_actual_memory_fallback(capsys):
    cfg = type("Config", (), {"mnemosyne_home": "configured", "exec_model": "model-v1"})()
    cli._print_banner(cfg, memory_name="local")
    assert "Memory: local" in capsys.readouterr().out


def test_banner_and_slash_status_do_not_print_raw_configured_model(capsys):
    secret_like = "ghp_" + "a" * 16
    cfg = type("Config", (), {"mnemosyne_home": None, "exec_model": secret_like})()
    hive = type("Hive", (), {"config": cfg, "memory": None})()
    cli._print_banner(cfg)
    banner = capsys.readouterr().out
    assert cli._handle_slash("/status", hive=hive, session_id="test-session")
    status = capsys.readouterr().out
    assert secret_like[:16] not in banner
    assert secret_like not in status
    assert "model=model" in status


def test_chat_ctrl_c_cancels_input_without_ending_session(monkeypatch, capsys):
    class FakeConfig:
        minimax_api_key = "sentinel"
        exec_model = "model-v1"
        exec_provider = "minimax"

    class FakeHive:
        config = FakeConfig()
        memory = type("Memory", (), {"name": "local"})()
        session_store = None

        async def aclose(self):
            pass

    calls = 0

    async def fake_read(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        raise EOFError

    monkeypatch.setattr("hive.core.config.HiveConfig.from_env", lambda: FakeConfig())
    monkeypatch.setattr("hive.runtime.HiveOS.build", lambda *args, **kwargs: FakeHive())
    monkeypatch.setattr("hive.surfaces.cli.repl_input.ChatInput.read", fake_read)
    assert asyncio.run(cli._chat()) == 0
    assert calls == 2
    assert "input cancelled" in capsys.readouterr().out
