"""Deterministic, authorization-aware Telegram command surface for Hive."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from hive.gateway.approval_decisions import ApprovalDecisionError, decide_approval
from hive.gateway.telegram_sessions import TelegramSessionBindings


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    name: str
    description: str
    usage: str
    owner_only: bool = False


@dataclass(frozen=True, slots=True)
class ParsedTelegramCommand:
    name: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    reply: str
    session_id: str


COMMANDS: tuple[TelegramCommand, ...] = (
    TelegramCommand("start", "Introduce Hive and show safe controls", "/start"),
    TelegramCommand("help", "Show command help", "/help [command]"),
    TelegramCommand("commands", "List available commands", "/commands"),
    TelegramCommand("new", "Start a new conversation without deleting history", "/new [title]"),
    TelegramCommand("reset", "Alias for a safe new conversation", "/reset [title]"),
    TelegramCommand("status", "Show current session and Hive status", "/status"),
    TelegramCommand("sessions", "List this chat's saved conversations", "/sessions"),
    TelegramCommand("resume", "Resume a listed conversation", "/resume <number>"),
    TelegramCommand("memory", "Show memory-layer status", "/memory"),
    TelegramCommand("tasks", "Show recent durable tasks", "/tasks"),
    TelegramCommand("approvals", "Show pending approvals", "/approvals"),
    TelegramCommand("reviews", "Show self-development proposals and evidence", "/reviews"),
    TelegramCommand("evals", "Show safe-learning evaluation evidence", "/evals"),
    TelegramCommand("correct", "Correct one canonical memory claim", "/correct <stable-key> | <claim> | <reason>", True),
    TelegramCommand("approve", "Approve a pending protected action", "/approve <approval-id>", True),
    TelegramCommand("deny", "Deny a pending protected action", "/deny <approval-id>", True),
)
_COMMANDS_BY_NAME = {command.name: command for command in COMMANDS}


def parse_command(text: str) -> ParsedTelegramCommand | None:
    """Parse a standalone Telegram slash command, including ``/name@bot`` form."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split()
    if not parts:
        return None
    name = parts[0].split("@", 1)[0].lower()
    if not name or not name.replace("_", "").isalnum():
        return None
    return ParsedTelegramCommand(name=name, args=tuple(parts[1:]))


def native_commands() -> list[dict[str, str]]:
    """Return Bot API-safe menu definitions from the one canonical registry."""
    return [{"command": command.name, "description": command.description[:256]}
            for command in COMMANDS]


class TelegramCommandService:
    """Dispatch safe chat commands locally; never forward one to the model."""

    def __init__(self, hive: Any, bindings: TelegramSessionBindings) -> None:
        self._hive = hive
        self._bindings = bindings

    def active_session(self, *, chat_id: str, user_id: str, thread_id: str,
                       legacy_session_id: str) -> str:
        return self._bindings.active_or_create(
            chat_id=chat_id, user_id=user_id, thread_id=thread_id,
            legacy_session_id=legacy_session_id,
        )

    async def dispatch(self, command: ParsedTelegramCommand, *, chat_id: str, user_id: str,
                 thread_id: str, legacy_session_id: str, is_owner: bool,
                 event_id: str = "") -> CommandResult:
        current = self.active_session(
            chat_id=chat_id, user_id=user_id, thread_id=thread_id,
            legacy_session_id=legacy_session_id,
        )
        spec = _COMMANDS_BY_NAME.get(command.name)
        if spec is None:
            return CommandResult(
                reply=f"Unknown command: /{command.name}. Use /commands.", session_id=current,
            )
        if spec.owner_only and not is_owner:
            return CommandResult(reply="This command is available only to the owner.", session_id=current)
        if command.name == "start":
            return CommandResult(
                reply="Hive is online. Use /commands to see deterministic controls, or write a normal message.",
                session_id=current,
            )
        if command.name in {"help", "commands"}:
            return CommandResult(reply=self._help(command.args), session_id=current)
        if command.name in {"new", "reset"}:
            title = " ".join(command.args).strip()[:80]
            session_id = self._bindings.new_session(
                chat_id=chat_id, user_id=user_id, thread_id=thread_id,
                legacy_session_id=legacy_session_id,
            )
            if title:
                self._hive.session_store.set_title(session_id, title)
            label = f' “{title}”' if title else ""
            return CommandResult(
                reply=f"Started a new conversation{label}. Previous history is preserved; use /sessions.",
                session_id=session_id,
            )
        if command.name == "status":
            return CommandResult(reply=self._status(current), session_id=current)
        if command.name == "sessions":
            return CommandResult(reply=self._sessions(chat_id, user_id, thread_id), session_id=current)
        if command.name == "resume":
            if len(command.args) != 1 or not command.args[0].isdigit():
                return CommandResult(reply="Usage: /resume <number>. Use /sessions first.", session_id=current)
            resumed = self._bindings.resume(
                chat_id=chat_id, user_id=user_id, thread_id=thread_id, index=int(command.args[0]),
            )
            if resumed is None:
                return CommandResult(reply="That session number does not exist. Use /sessions.", session_id=current)
            title = self._hive.session_store.get_title(resumed)
            suffix = f' “{title}”' if title else ""
            return CommandResult(reply=f"Resumed session {command.args[0]}{suffix}.", session_id=resumed)
        if command.name == "memory":
            return CommandResult(reply=self._memory(), session_id=current)
        if command.name == "tasks":
            return CommandResult(reply=self._tasks(), session_id=current)
        if command.name == "reviews":
            return CommandResult(reply=self._reviews(), session_id=current)
        if command.name == "evals":
            return CommandResult(reply=self._evals(), session_id=current)
        if command.name == "correct":
            return self._correct(command, current, chat_id=chat_id, user_id=user_id, event_id=event_id)
        if command.name == "approvals":
            return CommandResult(reply=self._approvals(), session_id=current)
        if command.name in {"approve", "deny"}:
            return await self._decide(command, current, user_id)
        raise AssertionError(f"registered command without handler: {command.name}")

    async def _decide(self, command: ParsedTelegramCommand, session_id: str,
                      user_id: str) -> CommandResult:
        if len(command.args) != 1:
            return CommandResult(
                reply=f"Usage: /{command.name} <approval-id>.", session_id=session_id,
            )
        approved = command.name == "approve"
        try:
            result = await decide_approval(
                self._hive, command.args[0], approved=approved,
                decided_by=f"human:telegram:{user_id}",
            )
        except ApprovalDecisionError as exc:
            return CommandResult(
                reply=f"Approval was not changed: {exc.detail}", session_id=session_id,
            )
        status = str(result.get("status", "completed"))
        if approved:
            return CommandResult(
                reply=f"Approval recorded. Result: {status}.", session_id=session_id,
            )
        return CommandResult(reply="Approval denied. No action was executed.", session_id=session_id)

    def _correct(self, command: ParsedTelegramCommand, session_id: str, *, chat_id: str,
                 user_id: str, event_id: str) -> CommandResult:
        raw = " ".join(command.args)
        parts = [part.strip() for part in raw.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            return CommandResult(
                reply="Usage: /correct <stable-key> | <claim> | <reason>.", session_id=session_id,
            )
        stable_key, content, reason = parts
        fallback = hashlib.sha256(f"{chat_id}\0{user_id}\0{raw}".encode("utf-8")).hexdigest()
        try:
            memory = self._hive.correct_memory_claim(
                stable_key=stable_key,
                content=content,
                source=f"telegram-owner:{user_id}",
                actor=f"human:telegram:{user_id}",
                reason=reason,
                idempotency_key=f"telegram-correction:{event_id or fallback}",
            )
        except KeyError:
            return CommandResult(reply="No canonical memory claim has that stable key.", session_id=session_id)
        except ValueError as exc:
            return CommandResult(reply=f"Memory correction was not recorded: {exc}.", session_id=session_id)
        return CommandResult(
            reply=f"Memory correction recorded as version {memory.version}. Earlier history is retained.",
            session_id=session_id,
        )
    def _help(self, args: tuple[str, ...]) -> str:
        if args:
            key = args[0].lstrip("/").lower()
            command = _COMMANDS_BY_NAME.get(key)
            if command is None:
                return f"Unknown command: /{key}. Use /commands."
            return f"{command.usage} — {command.description}"
        lines = ["Hive command surface:"]
        lines.extend(f"{command.usage} — {command.description}" for command in COMMANDS)
        return "\n".join(lines)

    def _status(self, session_id: str) -> str:
        title = self._hive.session_store.get_title(session_id) or "untitled"
        messages = self._hive.session_store.count_messages(session_id)
        memory = self._memory_counts()
        pending = self._hive.task_board.pending_count()
        approvals = len(self._hive.approval_store.pending())
        return (
            "Hive status\n"
            f"Session: {title}\n"
            f"Messages: {messages}\n"
            f"Memory records: {memory}\n"
            f"Pending tasks: {pending}\n"
            f"Pending approvals: {approvals}"
        )

    def _sessions(self, chat_id: str, user_id: str, thread_id: str) -> str:
        sessions = self._bindings.sessions(chat_id=chat_id, user_id=user_id, thread_id=thread_id)
        if not sessions:
            return "No saved conversations yet."
        lines = ["Your conversations:"]
        for index, item in enumerate(sessions[:10], start=1):
            title = self._hive.session_store.get_title(item.session_id) or "untitled"
            count = self._hive.session_store.count_messages(item.session_id)
            marker = " (current)" if item.active else ""
            lines.append(f"{index}. {title} — {count} messages{marker}")
        if len(sessions) > 10:
            lines.append("Only the 10 most recent conversations are shown.")
        lines.append("Use /resume <number> to switch without deleting history.")
        return "\n".join(lines)

    def _memory_counts(self) -> int:
        try:
            counts = self._hive.memory.count()
            return sum(int(value) for value in counts.values() if isinstance(value, int))
        except Exception:
            return 0

    def _memory(self) -> str:
        provider = getattr(self._hive.memory, "name", "local")
        status = getattr(self._hive, "memory_projection_status", None)
        summary = status() if callable(status) else {"open": 0, "requires_review": 0}
        return (
            f"Memory provider: {provider}\nMemory records: {self._memory_counts()}\n"
            f"Projection queue: {int(summary.get('open', 0))} open; "
            f"{int(summary.get('requires_review', 0))} require owner review.\n"
            "Use normal conversation to store or correct facts."
        )

    def _tasks(self) -> str:
        tasks = list(reversed(self._hive.task_board.all()[-5:]))
        if not tasks:
            return "No durable tasks recorded."
        lines = [f"Tasks (pending: {self._hive.task_board.pending_count()}):"]
        lines.extend(f"#{task.id} {task.state} — {task.kind}" for task in tasks)
        return "\n".join(lines)

    def _reviews(self) -> str:
        store = getattr(self._hive, "selfdev_runs", None)
        if store is None:
            return "No self-development proposals recorded."
        items = store.recent(limit=5)
        if not items:
            return "No self-development proposals recorded."
        lines = ["Self-development reviews:"]
        lines.extend(f"{item.run_id} — {item.state} ({item.risk})" for item in items)
        lines.append("These records are evidence only; no merge or deploy is automatic.")
        return "\\n".join(lines)

    def _evals(self) -> str:
        from hive.evals.safe_learning import SUITE_ID, SUITE_VERSION
        store = getattr(self._hive, "evaluation_evidence", None)
        if store is None:
            return "Safe-learning evaluations have no durable evidence yet."
        latest = store.latest(SUITE_ID, SUITE_VERSION)
        if latest is None:
            return "Safe-learning evaluations have not run yet; diagnosis remains gated."
        result = "passed" if latest.all_passed else "failed"
        return (
            "Safe-learning evaluations\n"
            f"Suite: {SUITE_ID} v{SUITE_VERSION}\n"
            f"Latest: {result} ({latest.passed}/{latest.total})\n"
            f"Offline-only: {'yes' if latest.offline_only else 'no'}\n"
            "This is evidence only; it never enables autonomy or sends messages."
        )

    def _approvals(self) -> str:
        pending = self._hive.approval_store.pending()
        if not pending:
            return "No pending approvals."
        lines = ["Pending approvals:"]
        lines.extend(f"{item.approval_id} — {item.kind}: {item.tool}" for item in pending[:10])
        lines.append("Owner only: /approve <approval-id> or /deny <approval-id>.")
        return "\n".join(lines)
