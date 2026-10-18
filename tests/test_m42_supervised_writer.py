"""M42 host-verified approval and one-shot REVIEW publication boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from hive.core.pr_review_auth import PrReviewAuthorizationStore
from hive.core.supervised_review_prepare import SupervisedReviewEvidencePreparer
from hive.observability.persistence import ObservabilityLedger
from hive.observability.supervised_review_writer import SupervisedReviewWriter
from tests.test_m41_supervised_review_prepare import (
    BRANCH, IMAGE, PATH, _Runner, _binding, _git, _run,
)


def _approved_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M42 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    db = tmp_path / "state.sqlite"
    binding, store = _binding(repo, db)
    candidate = _git(repo, "rev-parse", "HEAD")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, candidate)
    assert store.record_policy_checked_candidate(
        binding, candidate_commit=candidate, candidate_parent=binding.expected_head,
    )

    async def fresh(branch: str, head: str):
        return {"ok": branch == BRANCH and head == binding.expected_head,
                "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=db, runner=_Runner(), git_run=_run,
    )
    link = asyncio.run(preparer.prepare(binding, store, fresh))
    assert link is not None
    assert store.decide(
        link.request_id, store._binding_digest(binding),
        approved=True, principal="human:approver",
    )
    return repo, db, store, link


def test_approved_supervised_candidate_requires_all_durable_proofs(tmp_path):
    _repo, _db, store, link = _approved_candidate(tmp_path)
    approved = store.approved_supervised_candidate(link.request_id)
    assert approved is not None
    assert approved.binding == link.binding
    assert approved.candidate_commit == link.candidate_commit
    assert approved.evidence_binding == link.evidence_binding


@pytest.mark.parametrize("mutation", [
    "remove_link", "remove_policy", "corrupt_evidence", "corrupt_results",
    "finish_feedback", "drift_identity", "expire", "change_candidate", "standdown",
])
def test_approved_supervised_candidate_fails_closed_on_durable_drift(tmp_path, mutation):
    _repo, db, store, link = _approved_candidate(tmp_path)
    with sqlite3.connect(db) as conn:
        if mutation == "remove_link":
            conn.execute("DELETE FROM pr_review_host_evidence")
        elif mutation == "remove_policy":
            conn.execute("DELETE FROM pr_review_policy_evidence")
        elif mutation == "corrupt_evidence":
            conn.execute(
                "UPDATE candidate_evidence_receipts SET binding_json='{}'",
            )
        elif mutation == "corrupt_results":
            conn.execute(
                "UPDATE candidate_evidence_receipts SET check_results_json='[]'",
            )
        elif mutation == "finish_feedback":
            conn.execute("UPDATE selfmod_pr_feedback_rounds SET state='failed'")
        elif mutation == "drift_identity":
            conn.execute("UPDATE selfmod_pr_identity SET pushed_sha=?", ("f" * 40,))
        elif mutation == "expire":
            conn.execute("UPDATE pr_review_authorizations SET expires_at=0")
        elif mutation == "change_candidate":
            conn.execute("UPDATE pr_review_candidates SET candidate_commit=?", ("f" * 40,))
        elif mutation == "standdown":
            conn.execute(
                "INSERT INTO selfmod_pr_standdown "
                "(pr_url,run_id,marker,reason_code,state,reserved_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (link.binding.pr_url, link.binding.run_id, "marker", "manual", "reserved", 1, 1),
            )
    assert store.approved_supervised_candidate(link.request_id) is None


def test_legacy_approved_candidate_without_evidence_is_not_supervised(tmp_path):
    _repo, db, store, link = _approved_candidate(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM pr_review_host_evidence")
    assert store.approved_prepared_candidate(link.request_id) is not None
    assert store.approved_supervised_candidate(link.request_id) is None


def _writer_pushes(commands):
    return [
        command for command in commands
        if command and command[0] == "git" and "push" in command
    ]


def _writer_fixture(tmp_path, *, trusted_origin=True, push_fails=False,
                    image_digest=IMAGE, extra_push_origin=False,
                    delete_before_push=False):
    repo, db, store, link = _approved_candidate(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-q")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(
        repo, "push", "-q", "origin",
        f"{link.binding.expected_head}:refs/heads/{BRANCH}",
    )
    commands: list[tuple[str, ...]] = []

    async def audited_run(command, cwd):
        argv = tuple(command)
        commands.append(argv)
        if argv[:3] == ("git", "remote", "get-url"):
            if extra_push_origin and "--push" in argv:
                return 0, (
                    "https://github.com/Degi-ceo/HiveOS.git\n"
                    "https://example.invalid/other.git\n"
                )
            if trusted_origin:
                return 0, "https://github.com/Degi-ceo/HiveOS.git\n"
            return 0, "https://example.invalid/other.git\n"
        if delete_before_push and _writer_pushes([argv]):
            _git(repo, "push", "-q", "origin", f":refs/heads/{BRANCH}")
        if push_fails and _writer_pushes([argv]):
            return 1, "untrusted remote error text"
        return await _run(command, cwd)

    async def fresh(branch: str, head: str):
        binding = link.binding
        return {
            "ok": True, "number": binding.pr_number, "url": binding.pr_url,
            "state": "open", "head_sha": head, "head_ref": branch,
            "base_ref": binding.base_ref, "pr_id": binding.pr_id,
            "author_id": binding.author_id,
            "head_repo_id": binding.head_repo_id,
            "base_repo_id": binding.base_repo_id,
        }

    writer = SupervisedReviewWriter(
        repo_root=repo, state_db=db, pinned_image_digest=image_digest,
        git_run=audited_run,
    )
    return repo, remote, db, store, link, writer, fresh, commands


def test_writer_uses_one_nonforce_push_and_spends_decision(tmp_path):
    repo, remote, db, store, link, writer, fresh, commands = _writer_fixture(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "remote.origin.mirror", "true")
    _git(repo, "config", "push.followTags", "true")
    _git(repo, "tag", "-am", "fixture tag", "fixture-tag")
    result = asyncio.run(writer.publish(link.request_id, store, fresh))
    assert result == {
        "ok": True, "stage": "pushed", "head_sha": link.candidate_commit,
    }
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == link.candidate_commit
    assert _git(remote, "tag", "-l") == ""
    assert store.approved_supervised_candidate(link.request_id) is None
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(link.binding.pr_url)[0]["state"] == "pushed"
    finally:
        ledger.close()
    pushes = _writer_pushes(commands)
    assert pushes == [
        (
            "git", "-c", "core.hooksPath=/dev/null",
            "-c", "remote.origin.mirror=false",
            "-c", "push.followTags=false",
            "push", "--porcelain", "--no-follow-tags",
            f"--force-with-lease=refs/heads/{BRANCH}:{link.binding.expected_head}",
            "origin",
            f"{link.candidate_commit}:refs/heads/{BRANCH}",
        ),
    ]
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "declined",
    }
    assert len(_writer_pushes(commands)) == 1


def test_writer_cancellation_after_own_consumption_finishes_uncertain(
    tmp_path, monkeypatch,
):
    repo, remote, db, store, link, writer, fresh, commands = _writer_fixture(tmp_path)
    original_trusted_origin = writer._trusted_origin

    async def exercise():
        consumed = asyncio.Event()

        async def pause_after_consumption(binding):
            state = store.supervised_round_status(
                pr_url=binding.pr_url, expected_head=binding.expected_head,
                feedback_round=binding.feedback_round, run_id=binding.run_id,
            )
            if state is not None and state["state"] == "consumed":
                consumed.set()
                await asyncio.Event().wait()
            return await original_trusted_origin(binding)

        monkeypatch.setattr(writer, "_trusted_origin", pause_after_consumption)
        task = asyncio.create_task(writer.publish(link.request_id, store, fresh))
        await consumed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(link.binding.pr_url)[0]["state"] == "uncertain"
    finally:
        ledger.close()
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == link.binding.expected_head
    assert not _writer_pushes(commands)


@pytest.mark.parametrize("failure", [
    "untrusted_origin", "missing_remote_head", "revoked", "ref_drift",
    "wrong_live_identity", "missing_evidence", "wrong_image", "extra_push_origin",
])
def test_writer_declines_without_push_before_consumption(tmp_path, failure):
    repo, _remote, db, store, link, writer, fresh, commands = _writer_fixture(
        tmp_path, trusted_origin=failure != "untrusted_origin",
        image_digest="sha256:" + "f" * 64 if failure == "wrong_image" else IMAGE,
        extra_push_origin=failure == "extra_push_origin",
    )
    if failure == "missing_remote_head":
        _git(repo, "push", "-q", "origin", f":refs/heads/{BRANCH}")
    elif failure == "revoked":
        assert store.revoke(
            link.request_id, store._binding_digest(link.binding),
            principal="human:approver",
        )
    elif failure == "ref_drift":
        ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
            link.binding.canonical_json().encode("utf-8")
        ).hexdigest()
        _git(repo, "update-ref", ref, link.binding.expected_head)
    elif failure == "missing_evidence":
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM pr_review_host_evidence")
    elif failure == "wrong_live_identity":
        normal_fresh = fresh

        async def fresh(branch: str, head: str):
            observed = await normal_fresh(branch, head)
            observed["pr_id"] += 1
            return observed

    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "declined",
    }
    assert not _writer_pushes(commands)


def test_failed_push_is_terminal_and_never_retries(tmp_path):
    _repo, _remote, db, store, link, writer, fresh, commands = _writer_fixture(
        tmp_path, push_fails=True,
    )
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "push_uncertain",
    }
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "declined",
    }
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(link.binding.pr_url)[0]["state"] == "uncertain"
    finally:
        ledger.close()
    assert len(_writer_pushes(commands)) == 1


def test_deleted_branch_after_final_read_cannot_be_recreated(tmp_path):
    _repo, remote, db, store, link, writer, fresh, commands = _writer_fixture(
        tmp_path, delete_before_push=True,
    )
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "push_uncertain",
    }
    assert _git(remote, "branch", "--list", BRANCH) == ""
    assert len(_writer_pushes(commands)) == 1
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "declined",
    }
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(link.binding.pr_url)[0]["state"] == "uncertain"
    finally:
        ledger.close()


def test_remote_drift_after_consumption_stops_before_push(tmp_path, monkeypatch):
    repo, _remote, db, store, link, writer, fresh, commands = _writer_fixture(tmp_path)
    original_consume = store.consume_supervised

    def consume_then_remove_branch(approved):
        consumed = original_consume(approved)
        if consumed:
            _git(repo, "push", "-q", "origin", f":refs/heads/{BRANCH}")
        return consumed

    monkeypatch.setattr(store, "consume_supervised", consume_then_remove_branch)
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "push_uncertain",
    }
    assert not _writer_pushes(commands)
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(link.binding.pr_url)[0]["state"] == "uncertain"
    finally:
        ledger.close()


@pytest.mark.parametrize("mutation", [
    "finish_feedback", "drift_identity", "standdown", "remove_link", "expire",
])
def test_consume_supervised_rejects_state_changed_after_last_read(
    tmp_path, monkeypatch, mutation,
):
    _repo, _remote, db, store, link, writer, fresh, commands = _writer_fixture(tmp_path)
    original_consume = store.consume_supervised

    def mutate_then_consume(approved):
        with sqlite3.connect(db) as conn:
            if mutation == "finish_feedback":
                conn.execute("UPDATE selfmod_pr_feedback_rounds SET state='failed'")
            elif mutation == "drift_identity":
                conn.execute("UPDATE selfmod_pr_identity SET pushed_sha=?", ("f" * 40,))
            elif mutation == "standdown":
                conn.execute(
                    "INSERT INTO selfmod_pr_standdown "
                    "(pr_url,run_id,marker,reason_code,state,reserved_ts,updated_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (link.binding.pr_url, link.binding.run_id, "marker", "manual", "reserved", 1, 1),
                )
            elif mutation == "remove_link":
                conn.execute("DELETE FROM pr_review_host_evidence")
            elif mutation == "expire":
                conn.execute("UPDATE pr_review_authorizations SET expires_at=0")
        return original_consume(approved)

    monkeypatch.setattr(store, "consume_supervised", mutate_then_consume)
    assert asyncio.run(writer.publish(link.request_id, store, fresh)) == {
        "ok": False, "stage": "declined",
    }
    assert not _writer_pushes(commands)


def test_concurrent_writers_can_attempt_only_one_push(tmp_path):
    _repo, remote, _db, store, link, writer, fresh, commands = _writer_fixture(tmp_path)

    async def run_both():
        return await asyncio.gather(
            writer.publish(link.request_id, store, fresh),
            writer.publish(link.request_id, store, fresh),
        )

    results = asyncio.run(run_both())
    assert sorted(result["ok"] for result in results) == [False, True]
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == link.candidate_commit
    assert len(_writer_pushes(commands)) == 1
