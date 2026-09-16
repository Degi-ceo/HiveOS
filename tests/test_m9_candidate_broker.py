"""M9.4: coder proposals are review-bound and candidate-worktree-only."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from hive.agents.candidate_broker import CandidateBroker
from hive.core.run_context import bind_run_id
from hive.core.spec_search import EditOutcome, EditOp, RiskTier, SelfImprovement
from hive.observability.runs import RunLedger
from hive.tools.builtins import ProposeCandidateFile
from hive.tools.executor import ToolExecutor


class _Improver:
    def __init__(self) -> None:
        self.edits = []

    async def run(self, edits, *, dry_run=False):
        self.edits.extend(edits)
        return [EditOutcome(
            edit_id=edits[0].id, op=edits[0].op, tier=RiskTier.REVIEW,
            status="pending_approval", approval_id="approval-1",
        )]


class _Gate:
    def __init__(self) -> None:
        self.requests = []

    def request(self, name, args, reason):
        self.requests.append((name, args, reason))
        return "approval-1"


class _Modifier:
    def __init__(self) -> None:
        self.calls = []

    async def propose(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"ok": True, "stage": "dry_run"}


def test_coder_proposal_only_queues_review_and_never_writes_live_file(tmp_path):
    live = tmp_path / "src" / "hive"
    live.mkdir(parents=True)
    target = live / "module.py"
    target.write_text("old = 1\n", encoding="utf-8")
    improver = _Improver()
    broker = CandidateBroker(improver)

    outcome = asyncio.run(broker.propose_file(
        path="src/hive/module.py",
        expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        replacement="new = 2\n",
    ))

    assert outcome is not None and outcome.status == "pending_approval"
    assert target.read_text(encoding="utf-8") == "old = 1\n"
    assert len(improver.edits) == 1
    edit = improver.edits[0]
    assert edit.op is EditOp.PATCH_CODE
    assert edit.risk_tier is RiskTier.MANUAL  # policy overwrites it to REVIEW in run().
    assert edit.summary == "coder candidate proposal"
    assert edit.code is None


def test_coder_proposal_uses_real_review_gate_before_candidate_worktree_exists(tmp_path):
    live = tmp_path / "src" / "hive"
    live.mkdir(parents=True)
    target = live / "module.py"
    target.write_text("old = 1\n", encoding="utf-8")
    gate = _Gate()
    modifier = _Modifier()
    improver = SelfImprovement(modifier, gate=gate, safety_enabled=False)

    outcome = asyncio.run(CandidateBroker(improver).propose_file(
        path="src/hive/module.py",
        expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        replacement="new = 2\n",
    ))

    assert outcome is not None and outcome.status == "pending_approval"
    assert gate.requests and gate.requests[0][0] == "self_mod:patch_code"
    assert modifier.calls == []
    assert improver.get_pending("approval-1") is not None
    assert target.read_text(encoding="utf-8") == "old = 1\n"


def test_candidate_apply_replaces_only_matching_file_in_candidate_worktree(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")
    improver = _Improver()
    broker = CandidateBroker(improver)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    asyncio.run(broker.propose_file(
        path="src/hive/module.py", expected_sha256=digest, replacement="new = 2\n",
    ))
    changed = asyncio.run(improver.edits[0].apply(str(candidate)))

    assert changed == ["src/hive/module.py"]
    assert target.read_text(encoding="utf-8") == "new = 2\n"


def test_candidate_apply_refuses_stale_content_without_writing(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "tests" / "test_module.py"
    target.parent.mkdir(parents=True)
    target.write_text("changed elsewhere\n", encoding="utf-8")
    improver = _Improver()
    broker = CandidateBroker(improver)

    asyncio.run(broker.propose_file(
        path="tests/test_module.py",
        expected_sha256=hashlib.sha256(b"old\n").hexdigest(), replacement="new\n",
    ))

    assert asyncio.run(improver.edits[0].apply(str(candidate))) == []
    assert target.read_text(encoding="utf-8") == "changed elsewhere\n"


def test_candidate_check_runs_only_in_candidate_and_failure_reverts_change(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")

    class _Runner:
        async def run(self, worktree, argv):
            assert worktree == str(candidate)
            assert argv == ("python", "-m", "compileall", "src/hive")
            return 1, "must not persist"

    improver = _Improver()
    asyncio.run(CandidateBroker(improver, _Runner()).propose_file(
        path="src/hive/module.py", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        replacement="new = 2\n", checks=[("python", "-m", "compileall", "src/hive")],
    ))
    assert asyncio.run(improver.edits[0].apply(str(candidate))) == []
    assert target.read_text(encoding="utf-8") == "old = 1\n"


def test_candidate_check_audit_excludes_argv_and_output(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")
    audit = []

    class _Runner:
        image_reference_sha256 = "image-reference-digest"

        async def run(self, _worktree, _argv):
            return 0, "secret command output"

    improver = _Improver()
    asyncio.run(CandidateBroker(improver, _Runner(), audit=audit.append).propose_file(
        path="src/hive/module.py", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        replacement="new = 2\n", checks=[("python", "-m", "compileall", "src/hive")],
    ))
    assert asyncio.run(improver.edits[0].apply(str(candidate))) == ["src/hive/module.py"]
    serialized = str(audit)
    assert "src/hive" not in serialized and "secret command output" not in serialized


def test_candidate_check_lifecycle_is_durable_and_excludes_raw_candidate_data(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")
    ledger = RunLedger(tmp_path / "state.sqlite")
    ledger.begin("candidate-parent", kind="conversation", session_id="private-session")

    class _Runner:
        image_reference_sha256 = "private-image-reference"

        async def run(self, _worktree, _argv):
            return 0, "private candidate output"

    improver = _Improver()
    broker = CandidateBroker(improver, _Runner(), operator_event=ledger.record_operator_event)
    try:
        with bind_run_id("candidate-parent"):
            asyncio.run(broker.propose_file(
                path="src/hive/module.py", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                replacement="new = 2\n", checks=[("python", "-m", "compileall", "src/hive")],
            ))
        assert asyncio.run(improver.edits[0].apply(str(candidate))) == ["src/hive/module.py"]
        events = ledger.public_events("candidate-parent")
    finally:
        ledger.close()

    assert [(event["type"], event["data"]["status"]) for event in events] == [
        ("candidate_check", "started"), ("candidate_check", "passed"),
    ]
    assert events[-1]["data"]["check_kind"] == "compileall"
    assert events[-1]["data"]["duration_ms"] >= 0
    rendered = str(events)
    for private in ("src/hive", "private candidate output", "private-image-reference", "private-session"):
        assert private not in rendered


def test_candidate_check_kind_never_includes_allowed_argv_path(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")
    events = []
    audit = []

    class _Runner:
        image_reference_sha256 = "private-image"

        async def run(self, _worktree, _argv):
            return 0, "private output"

    improver = _Improver()
    broker = CandidateBroker(improver, _Runner(), operator_event=events.append, audit=audit.append)
    with bind_run_id("candidate-run"):
        asyncio.run(broker.propose_file(
            path="src/hive/module.py", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            replacement="new = 2\n", checks=[("ruff", "check", "tests/private_name.py")],
        ))
    assert asyncio.run(improver.edits[0].apply(str(candidate))) == ["src/hive/module.py"]
    assert {event["check_kind"] for event in events} == {"ruff"}
    assert "private_name" not in str(events)
    assert audit[0]["args"]["command_kind"] == "ruff"
    assert "private_name" not in str(audit)


def test_cancelled_candidate_check_records_terminal_lifecycle_then_reraises(tmp_path):
    candidate = tmp_path / "candidate"
    target = candidate / "src" / "hive" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("old = 1\n", encoding="utf-8")
    events = []

    class _Runner:
        image_reference_sha256 = "private-image"

        async def run(self, _worktree, _argv):
            raise asyncio.CancelledError()

    improver = _Improver()
    broker = CandidateBroker(improver, _Runner(), operator_event=events.append)
    with bind_run_id("candidate-run"):
        asyncio.run(broker.propose_file(
            path="src/hive/module.py", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            replacement="new = 2\n", checks=[("python", "-m", "compileall", "src/hive")],
        ))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(improver.edits[0].apply(str(candidate)))

    assert [event["status"] for event in events] == ["started", "cancelled"]
    assert target.read_text(encoding="utf-8") == "old = 1\n"


def test_candidate_checks_reject_invalid_outer_shape():
    with pytest.raises(ValueError):
        asyncio.run(CandidateBroker(_Improver()).propose_file(
            path="src/hive/x.py", expected_sha256="0" * 64, replacement="x", checks=None,
        ))


def test_candidate_apply_rejects_an_intermediate_symlink_before_writing(tmp_path):
    candidate = tmp_path / "candidate"
    redirected = candidate / "redirected"
    redirected.mkdir(parents=True)
    target = redirected / "module.py"
    target.write_text("old = 1\n", encoding="utf-8")
    try:
        (candidate / "src").symlink_to(redirected, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this test host: {exc}")
    improver = _Improver()
    broker = CandidateBroker(improver)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    asyncio.run(broker.propose_file(
        path="src/module.py", expected_sha256=digest, replacement="new = 2\n",
    ))

    assert asyncio.run(improver.edits[0].apply(str(candidate))) == []
    assert target.read_text(encoding="utf-8") == "old = 1\n"


def test_unbound_broker_fails_closed():
    assert asyncio.run(CandidateBroker().propose_file(
        path="src/hive/module.py", expected_sha256="0" * 64, replacement="x\n",
    )) is None


@pytest.mark.parametrize("path", ["../src/hive/x.py", "C:/src/hive/x.py", "docs/x.md"])
def test_broker_rejects_non_candidate_targets(path):
    with pytest.raises(ValueError):
        asyncio.run(CandidateBroker(_Improver()).propose_file(
            path=path, expected_sha256="0" * 64, replacement="x\n",
        ))


def test_broker_rejects_bad_digest_and_oversized_content_without_queueing():
    improver = _Improver()
    broker = CandidateBroker(improver)

    assert asyncio.run(broker.propose_file(
        path="src/hive/x.py", expected_sha256="bad", replacement="x",
    )) is None
    assert asyncio.run(broker.propose_file(
        path="src/hive/x.py", expected_sha256="0" * 64, replacement="x" * 100_001,
    )) is None
    assert not improver.edits


def test_coder_tool_hides_replacement_and_reports_only_review_boundary():
    improver = _Improver()
    tool = ProposeCandidateFile(CandidateBroker(improver))

    result = asyncio.run(tool.execute(
        path="src/hive/x.py", expected_sha256="0" * 64,
        replacement="api_key = do-not-display",
    ))

    assert result.success
    assert result.content == "[candidate proposal awaiting independent review]"
    assert "do-not-display" not in result.content


def test_coder_replacement_is_excluded_from_executor_audit_and_trace_payloads():
    improver = _Improver()
    tool = ProposeCandidateFile(CandidateBroker(improver))
    audit_entries = []
    executor = ToolExecutor({"propose_candidate_file": tool}, audit=audit_entries.append)
    replacement = "generated_secret_but_not_a_configured_secret"

    dispatch = asyncio.run(executor.execute("propose_candidate_file", {
        "path": "src/hive/x.py", "expected_sha256": "0" * 64,
        "replacement": replacement,
    }))

    assert dispatch.result is not None and dispatch.result.success
    assert len(audit_entries) == 1
    serialized = str(audit_entries[0])
    assert replacement not in serialized
    assert audit_entries[0]["args"]["replacement_bytes"] == len(replacement.encode("utf-8"))
