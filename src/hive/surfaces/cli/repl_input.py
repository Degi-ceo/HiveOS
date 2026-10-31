"""Interactive chat input backed by the shared conversation store."""

from __future__ import annotations

import hashlib
import re
import sys

_SLASH_COMMANDS = ("/help", "/status", "/session", "/clear", "/quit", "/exit")
_HISTORY_ROWS = 200
_MODEL_NAME = re.compile(r"[A-Za-z0-9._/-]{1,24}\Z")


def _toolbar(*, model: str, session_id: str, history_enabled: bool, loaded: int) -> str:
    """Display only bounded, non-content metadata in the editor toolbar."""
    safe_model = model if _MODEL_NAME.fullmatch(model) else "model"
    session_ref = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    history = f"history: {loaded} loaded" if history_enabled else "history: off"
    return f"Hive | {safe_model} | session: {session_ref} | {history} | /help"


class ChatInput:
    """Use prompt_toolkit on a real terminal and plain input for pipes/tests.

    Prior user turns are loaded only on explicit opt-in for a named session.
    The editor never writes a second history file or imports other sessions.
    """

    def __init__(self, *, session_store, session_id: str, model: str,
                 history_enabled: bool = False) -> None:
        self._prompt = None
        self._toolbar = ""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return

        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import InMemoryHistory

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):
                prefix = document.text_before_cursor
                if not prefix.startswith("/") or any(char.isspace() for char in prefix):
                    return
                for command in _SLASH_COMMANDS:
                    if command.startswith(prefix.lower()):
                        yield Completion(command, start_position=-len(prefix))

        history = InMemoryHistory()
        loaded = 0
        if history_enabled:
            # messages() coerces unknown stored roles to USER for model tolerance.
            # Input history must inspect the exact stored role instead.
            for row in session_store.message_details(session_id, limit=_HISTORY_ROWS):
                content = row.get("content")
                if row.get("role") == "user" and isinstance(content, str) and content:
                    history.append_string(content)
                    loaded += 1
        self._toolbar = _toolbar(
            model=model, session_id=session_id,
            history_enabled=history_enabled, loaded=loaded,
        )
        self._prompt = PromptSession(history=history, completer=SlashCompleter())

    async def read(self) -> str:
        if self._prompt is not None:
            return await self._prompt.prompt_async("you> ", bottom_toolbar=self._toolbar)
        return input("you> ")
