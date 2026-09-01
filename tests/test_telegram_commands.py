"""Contract tests for deterministic Telegram commands."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    return asyncio.run(service.dispatch(
        command, chat_id="42", user_id="7", thread_id="",
        legacy_session_id="telegram:42:7", is_owner=True,
    ))


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
    assert "Projection queue: 0 open; 0 require owner review." in _dispatch(service, "/memory").reply
    assert "#1 pending — review" in _dispatch(service, "/tasks").reply
    approvals = _dispatch(service, "/approvals").reply
    assert "approval-1 — tool: deploy" in approvals
    assert "never-render" not in approvals
    assert "Unknown command" in _dispatch(service, "/does_not_exist").reply


def test_autonomy_command_is_pull_only_and_never_claims_escalation(tmp_path):
    hive, service = _service(tmp_path)
    hive.autonomy_policy_status = lambda: {
        "policy_version": "1",
        "learning_mode": "evidence_only_never_escalates",
        "decision_counts": {"owner_approval": 2, "notify_only": 3},
    }

    reply = _dispatch(service, "/autonomy").reply

    assert "Owner approval required: 2" in reply
    assert "Notify-only/manual: 3" in reply
    assert "never expand Hive's permissions" in reply


def test_help_is_generated_from_the_central_registry(tmp_path):
    _, service = _service(tmp_path)
    reply = _dispatch(service, "/help resume").reply
    assert reply.startswith("/resume <number>")
    all_commands = _dispatch(service, "/commands").reply
    assert "/new [title]" in all_commands and "/approvals" in all_commands

def test_approval_commands_require_owner_and_never_render_approval_args(tmp_path, monkeypatch):
    _, service = _service(tmp_path)
    denied = asyncio.run(service.dispatch(
        parse_command("/approve approval-1"), chat_id="42", user_id="8", thread_id="",
        legacy_session_id="telegram:42:8", is_owner=False,
    ))
    assert "only to the owner" in denied.reply

    calls = []

    async def fake_decide(hive, approval_id, *, approved, decided_by):
        calls.append((approval_id, approved, decided_by))
        return {"executed": False, "status": "rejected"}

    monkeypatch.setattr("hive.gateway.telegram_commands.decide_approval", fake_decide)
    approved = _dispatch(service, "/approve approval-1")
    denied_by_owner = _dispatch(service, "/deny approval-2")
    assert "Result: rejected" in approved.reply
    assert "No action was executed" in denied_by_owner.reply
    assert calls == [
        ("approval-1", True, "human:telegram:7"),
        ("approval-2", False, "human:telegram:7"),
    ]


def test_correct_command_is_owner_only_and_uses_a_derived_human_actor(tmp_path):
    hive, service = _service(tmp_path)
    hive.correct_memory_claim = MagicMock(return_value=SimpleNamespace(version=2))
    denied = asyncio.run(service.dispatch(
        parse_command("/correct fact:owner | Kamil | Owner correction"),
        chat_id="42", user_id="8", thread_id="", legacy_session_id="telegram:42:8",
        is_owner=False, event_id="88",
    ))
    recorded = asyncio.run(service.dispatch(
        parse_command("/correct fact:owner | Kamil | Owner correction"),
        chat_id="42", user_id="7", thread_id="", legacy_session_id="telegram:42:7",
        is_owner=True, event_id="77",
    ))

    assert "only to the owner" in denied.reply
    assert "version 2" in recorded.reply
    assert "Kamil" not in recorded.reply
    assert hive.correct_memory_claim.call_args.kwargs == {
        "stable_key": "fact:owner",
        "content": "Kamil",
        "source": "telegram-owner:7",
        "actor": "human:telegram:7",
        "reason": "Owner correction",
        "idempotency_key": "telegram-correction:77",
    }
def test_reviews_command_renders_only_safe_proposal_metadata(tmp_path):
    from hive.core.selfdev_store import SelfDevelopmentStore

    hive, service = _service(tmp_path)
    hive.selfdev_runs = SelfDevelopmentStore(tmp_path / "state.sqlite")
    proposal = hive.selfdev_runs.propose(
        symptom="secret symptom must not render", plan="run focused tests", rationale="evidence",
    )
    reply = _dispatch(service, "/reviews").reply

    assert proposal.run_id in reply
    assert "requires_review" in reply
    assert "secret symptom" not in reply
    assert "no merge or deploy is automatic" in reply


def test_evals_command_renders_only_aggregate_evidence(tmp_path):
    from hive.evals.evidence_store import EvaluationEvidenceStore

    hive, service = _service(tmp_path)
    hive.evaluation_evidence = EvaluationEvidenceStore(tmp_path / "state.sqlite")
    hive.evaluation_evidence.record(
        suite_id="telegram-safe-learning", suite_version=1, manifest_digest="x" * 64,
        total=5, passed=5, failed=0, errored=0, offline_only=True, started_ts=1.0,
    )
    reply = _dispatch(service, "/evals").reply
    assert "telegram-safe-learning v1" in reply
    assert "passed (5/5)" in reply
    assert "autonomy" in reply
