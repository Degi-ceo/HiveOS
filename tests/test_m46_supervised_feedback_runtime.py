"""Runtime feedback must preserve an out-of-band REVIEW decision boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hive.core.config import HiveConfig
from hive.core.pr_review_auth import PrReviewAuthorizationStore
from hive.core.pr_review import ReviewSelection, ReviewSuggestion
from hive.core.self_mod import CandidateFailure, SelfModifier, _default_run
from hive.core.spec_search import Edit, EditOp
from hive.core.supervised_review_policy import qualify_supervised_review_candidate
from hive.core.supervised_review_prepare import SupervisedReviewEvidencePreparer
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from tests.test_m21_ci_repair_controller import (
    OLD,
    URL,
    Ledger,
    Observer,
    Repairer,
    _snapshot,
)
from tests.test_m21_feedback_runtime import (
    BRANCH as RUNTIME_BRANCH,
    NEW as RUNTIME_NEW,
    OLD as RUNTIME_OLD,
    URL as RUNTIME_URL,
    _Ledger as RuntimeLedger,
    _Observer as RuntimeObserver,
    _snapshot as runtime_snapshot,
)
from tests.test_m35_pr_review_prepare import (
    BRANCH,
    COMMIT,
    HEAD,
    PATH,
    TREE,
    FakeGit,
    _context,
    _gate,
)
from tests.test_m41_supervised_review_prepare import _Runner as PinnedTestRunner
from tests.test_m42_supervised_writer import _approved_candidate

from hive.core.pr_feedback import repair_failed_ci_once


def test_review_pending_keeps_the_exact_feedback_round_reserved():
    ledger = Ledger()
    observer = Observer()
    request_id = "2b017df1-94f0-4eeb-ab7a-1a9731468d9a"
    repairer = Repairer({"ok": True, "stage": "review_pending", "request_id": request_id})

    result = asyncio.run(repair_failed_ci_once(
        ledger, observer, repairer, run_id="run-42", pr_url=URL,
        snapshot=_snapshot(OLD), allow_review_pending=True,
    ))

    assert result == {"status": "review_pending", "round": 1, "request_id": request_id}
    assert ledger.rounds == [{"round": 1, "state": "reserved"}]
    assert ledger.finished == []
    assert repairer.calls == [("hive/auto-test", OLD)]
    assert observer.calls == [42]


def test_mixed_existing_pr_repair_retains_source_candidate_without_push(tmp_path):
    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": (branch, head) == (BRANCH, HEAD), "branch": branch, "head_sha": head}

    async def repair(failure):
        assert failure.changed_paths == (PATH,)

        async def apply(_worktree):
            return [PATH]

        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.repair_existing_pr_supervised_or_auto(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store,
        auto_candidate_gate=_gate,
        review_candidate_gate=qualify_supervised_review_candidate,
    ))

    assert result["ok"] is True and result["stage"] == "prepared"
    assert (result["path"], result["candidate_tree"], result["head_sha"]) == (
        PATH, TREE, COMMIT,
    )
    assert git.pushes == 0
    assert store.policy_checked_candidate(context.bind_candidate(PATH, TREE)) == {
        "candidate_commit": COMMIT, "candidate_parent": HEAD,
    }
    assert store.public_pending() == []


def test_review_repair_generator_changes_only_one_eligible_python_file(tmp_path):
    class Router:
        def __init__(self):
            self.calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            return CompletionResult(
                text=json.dumps({"old_text": "value = 1", "new_text": "value = 2"}),
                model="test-model",
            )

        async def aclose(self):
            pass

    async def unused_apply(_worktree):
        return []

    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    router = Router()
    hive = HiveOS.build(config, router=router)
    try:
        edit = Edit(
            op=EditOp.PATCH_CODE, summary="Repair one failed Python check",
            apply=unused_apply, target_files=[PATH],
        )
        assert hive.feedback_review_repair_factory(replace(
            edit, target_files=["tests/conftest.py"],
        )) is None
        assert hive.feedback_review_repair_factory(replace(
            edit, target_files=[PATH, "tests/other.py"],
        )) is None
        generator = hive.feedback_review_repair_factory(edit)
        assert generator is not None
        failure = CandidateFailure(
            attempt=0, test_log="FAILED one assertion", fingerprint="fp",
            staged_diff="+value = 1", changed_paths=(PATH,),
        )
        apply = asyncio.run(generator(failure))
        assert apply is not None and router.calls == 1
        candidate = tmp_path / "candidate"
        target = candidate / PATH
        target.parent.mkdir(parents=True)
        target.write_text("value = 1\n", encoding="utf-8")
        assert asyncio.run(apply(str(candidate))) == [PATH]
        assert target.read_text(encoding="utf-8") == "value = 2\n"
    finally:
        asyncio.run(hive.aclose())


@pytest.mark.parametrize("feedback_kind", ["ci", "review"])
def test_real_git_feedback_candidate_publishes_once_after_restart(tmp_path, feedback_kind):
    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"
    repo.mkdir()
    branch = "hive/auto-" + uuid.uuid4().hex
    pr_url = "https://github.com/owner/repo/pull/42"
    run_id = str(uuid.uuid4())

    def git(directory: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=directory, check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    git(repo, "init", "-q")
    git(repo, "config", "user.email", "hive@example.invalid")
    git(repo, "config", "user.name", "Hive M46 Test")
    (repo / ".gitignore").write_text(".worktrees/\ndata/\n", encoding="utf-8")
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 0\n", encoding="utf-8")
    test_file = repo / "tests" / "test_repair.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        f"    source = Path(__file__).resolve().parents[1] / '{PATH}'\n"
        "    assert source.read_text().strip() == 'value = 2'\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    source.write_text("value = 1\n", encoding="utf-8")
    git(repo, "commit", "-qam", "failing source head")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "-M", "main")
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    class Router:
        async def complete(self, _messages, **_kwargs):
            return CompletionResult(
                text=json.dumps({"old_text": "value = 1", "new_text": "value = 2"}),
                model="test-model",
            )

        async def aclose(self):
            pass

    class Observer:
        available = True

        async def observe(self, number):
            assert number == 42
            return SimpleNamespace(as_dict=lambda: dict(snapshot))

    snapshot = {
        "number": 42, "url": pr_url, "state": "open",
        "status": "draft" if feedback_kind == "ci" else "changes_requested",
        "draft": feedback_kind == "ci",
        "ci_state": "failed" if feedback_kind == "ci" else "passed",
        "ownership_verified": True,
        "head_sha": head, "pr_id": 99, "author_id": 4,
        "head_repo_id": 8, "base_repo_id": 8,
        "head_ref": branch, "base_ref": "main",
    }
    config = replace(
        HiveConfig.from_env(root=repo, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    repair_results = []

    def build_hive():
        hive = HiveOS.build(config, router=Router())
        hive.config = replace(
            hive.config, pr_feedback_enabled=True, github_owner="owner",
            github_repo="repo", github_token="test-token",
            pr_reviewer_ids=frozenset({"1234"}) if feedback_kind == "review" else frozenset(),
        )
        hive.pr_observer = Observer()
        if feedback_kind == "review":
            suggestion = ReviewSuggestion(
                thread_id="thread-1", comment_id="comment-1", reviewer_id=1234,
                path=PATH, line=1, replacement="value = 2", head_sha=head,
                signal_digest="d" * 64,
            )

            class Reader:
                async def select(self, number, sha, reviewers):
                    assert (number, sha, reviewers) == (42, head, frozenset({1234}))
                    return ReviewSelection("ready", suggestion)

            hive.pr_review_reader = Reader()
            hive.pr_commenter = object()
        hive.self_modifier = SelfModifier(
            repo_root=str(repo), run=_default_run,
            test_cmd="python -B -m pytest -q -p no:cacheprovider tests/test_repair.py",
        )
        method = (
            "repair_existing_pr_supervised_or_auto" if feedback_kind == "ci"
            else "apply_existing_pr_review"
        )
        original_repair = getattr(hive.self_modifier, method)

        async def traced_repair(*args, **kwargs):
            result = await original_repair(*args, **kwargs)
            repair_results.append(result)
            return result

        setattr(hive.self_modifier, method, traced_repair)
        hive.supervised_review_preparer = SupervisedReviewEvidencePreparer(
            repo_root=repo, state_db=config.state_db, runner=PinnedTestRunner(),
            git_run=_default_run,
        )
        hive.supervised_review_writer = object()
        return hive

    hive = build_hive()
    try:
        receipt = {
            "url": pr_url, "number": 42, "pr_id": 99, "author_id": 4,
            "head_repo_id": 8, "base_repo_id": 8, "head_ref": branch,
            "head_sha": head, "base_ref": "main",
        }
        hive.observability_ledger.record_selfmod({
            "run_id": run_id, "title": "candidate", "branch": branch,
            "pr_url": pr_url, "head_sha": head, "stage": "pushed", "ok": True,
            "pr_creation": receipt,
        })
        assert hive.observability_ledger.bind_pr_identity(run_id, pr_url, snapshot)
        first = asyncio.run(hive.react_to_failed_pr_ci(snapshot))
        assert first["status"] == "review_pending", (first, repair_results)
        assert len(hive.pr_review_authorizations.public_pending()) == 1
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == head
        rows = hive.observability_ledger.get_pr_feedback_rounds(pr_url)
        assert rows[0]["state"] == "reserved"
    finally:
        asyncio.run(hive.aclose())

    project_root = Path(__file__).resolve().parents[1]
    worker_env = {
        name: value for name in ("PATH", "SystemRoot", "TEMP", "TMP", "USERPROFILE")
        if (value := os.environ.get(name)) is not None
    }
    worker_env.update({
        "PYTHONPATH": str(project_root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
    })
    worker = subprocess.run(
        [sys.executable, "-m", "tests.m49_feedback_worker", str(repo), branch, head,
         feedback_kind],
        cwd=project_root, env=worker_env, check=False, capture_output=True, text=True,
    )
    assert worker.returncode == 0, worker.stderr
    results = [line.removeprefix("M49_RESULT=") for line in worker.stdout.splitlines()
               if line.startswith("M49_RESULT=")]
    assert len(results) == 1, worker.stdout
    published = json.loads(results[0])
    assert published["status"] == "pushed", published
    assert published["round"] == 1
    assert published["head_sha"] == repair_results[0]["head_sha"] != head
    assert published["push_attempts"] == 1
    assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == published["head_sha"]
    assert len(repair_results) == 1

    snapshot.update({"head_sha": published["head_sha"], "ci_state": "pending"})
    restarted = build_hive()
    try:
        assert restarted.observability_ledger.get_pr_feedback_rounds(pr_url)[0]["state"] == "pushed"
        assert restarted.pr_review_authorizations.approved_supervised_candidate(first["request_id"]) is None
        assert asyncio.run(restarted.react_to_failed_pr_ci(snapshot)) == {"status": "wait"}
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == published["head_sha"]
        assert len(repair_results) == 1
    finally:
        asyncio.run(restarted.aclose())


@pytest.fixture
def review_runtime(tmp_path, monkeypatch):
    run_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())

    class ReviewLedger(RuntimeLedger):
        def __init__(self):
            super().__init__()
            self.finished = []

        def find_selfmod_run_id(self, *, pr_url):
            assert pr_url == RUNTIME_URL
            return run_id

        def validate_pr_identity(self, candidate_run, pr_url, snapshot):
            return candidate_run == run_id and super().validate_pr_identity(
                "run-42", pr_url, snapshot,
            )

        def get_pr_identity(self, pr_url):
            return {**super().get_pr_identity(pr_url), "run_id": run_id}

        def reserve_pr_feedback_round(self, candidate_run, pr_url, snapshot, *, feedback_key):
            assert candidate_run == run_id and pr_url == RUNTIME_URL
            assert self.validate_pr_identity(candidate_run, pr_url, snapshot)
            assert feedback_key == f"ci:{RUNTIME_OLD}"
            self.reservations += 1
            row = {
                "round": 1, "run_id": run_id, "state": "reserved",
                "expected_sha": RUNTIME_OLD,
                "feedback_key": hashlib.sha256(feedback_key.encode()).hexdigest(),
            }
            self.rounds.append(row)
            return dict(row)

        def finish_pr_feedback_round(self, pr_url, round_number, *, state, new_sha=""):
            self.finished.append((pr_url, round_number, state, new_sha))
            return super().finish_pr_feedback_round(
                pr_url, round_number, state=state, new_sha=new_sha,
            )

    class Modifier:
        def __init__(self):
            self.calls = 0
            self.pushes = 0

        async def repair_existing_pr_supervised_or_auto(
            self, branch, head, verify, repair, **kwargs,
        ):
            self.calls += 1
            assert (branch, head) == (RUNTIME_BRANCH, RUNTIME_OLD)
            assert kwargs["review_context"].run_id == run_id
            assert await verify(branch, head) == {
                "ok": True, "branch": branch, "head_sha": head,
            }
            apply = await repair(CandidateFailure(
                attempt=0, test_log="FAILED one check", fingerprint="fp",
                staged_diff="+value = 1", changed_paths=(PATH,),
            ))
            assert apply is not None
            return {
                "ok": True, "stage": "prepared", "path": PATH,
                "candidate_tree": TREE, "head_sha": COMMIT,
            }

    class Preparer:
        def __init__(self):
            self.calls = 0

        async def prepare(self, binding, _authorizations, verify):
            self.calls += 1
            assert binding.path == PATH and binding.run_id == run_id
            assert await verify(binding.branch, binding.expected_head) == {
                "ok": True, "branch": RUNTIME_BRANCH, "head_sha": RUNTIME_OLD,
            }
            return SimpleNamespace(request_id=request_id)

    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    hive = HiveOS.build(config)
    hive.config = replace(
        hive.config, pr_feedback_enabled=True, github_owner="owner",
        github_repo="repo", github_token="test-token",
    )
    ledger = ReviewLedger()
    for name in (
        "find_selfmod_run_id", "validate_pr_identity", "get_pr_identity",
        "get_pr_feedback_rounds", "get_pr_standdown", "reserve_pr_feedback_round",
        "finish_pr_feedback_round", "reserve_pr_standdown", "mark_pr_standdown",
    ):
        monkeypatch.setattr(hive.observability_ledger, name, getattr(ledger, name))
    hive.pr_observer = RuntimeObserver(move_head_on_third=False)
    modifier, preparer = Modifier(), Preparer()
    hive.self_modifier = modifier
    hive.supervised_review_preparer = preparer
    hive.supervised_review_writer = object()
    generator_calls = []

    def review_factory(edit):
        assert edit.op is EditOp.PATCH_CODE and edit.target_files == [PATH]
        generator_calls.append(edit)

        async def generate(_failure):
            async def apply(_worktree):
                return [PATH]

            return apply

        return generate

    hive.feedback_review_repair_factory = review_factory
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "pending"},
    )
    try:
        yield hive, ledger, modifier, preparer, generator_calls, request_id
    finally:
        asyncio.run(hive.aclose())


def test_runtime_source_candidate_waits_for_approver_without_duplicate_generation(review_runtime):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    first = asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))
    assert first == {"status": "review_pending", "round": 1, "request_id": request_id}
    assert ledger.rounds[0]["state"] == "reserved" and ledger.reservations == 1
    assert ledger.finished == []
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)

    second = asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))
    assert second == {**first, "resumed": True}
    assert ledger.reservations == 1
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def _configure_source_review(review_runtime, monkeypatch):
    hive, ledger, _modifier, preparer, _generator_calls, request_id = review_runtime
    suggestion = ReviewSuggestion(
        thread_id="thread-1", comment_id="comment-1", reviewer_id=1234,
        path=PATH, line=1, replacement="value = 2", head_sha=RUNTIME_OLD,
        signal_digest="d" * 64,
    )
    review_key = hashlib.sha256(f"review:{suggestion.signal_digest}".encode()).hexdigest()
    snapshot = runtime_snapshot(
        draft=False, status="changes_requested", ci_state="passed",
    )

    class Observer:
        available = True

        async def observe(self, number):
            assert number == 42
            return SimpleNamespace(as_dict=lambda: dict(snapshot))

    class Reader:
        async def select(self, number, sha, reviewers):
            assert (number, sha, reviewers) == (42, RUNTIME_OLD, frozenset({1234}))
            return ReviewSelection("ready", suggestion)

    class Modifier:
        calls = 0

        async def apply_existing_pr_review(self, branch, sha, verify, apply, **kwargs):
            self.calls += 1
            assert kwargs["review_context"].feedback_key_digest == review_key
            assert kwargs["authorizations"] is hive.pr_review_authorizations
            assert await verify(branch, sha) == {
                "ok": True, "branch": branch, "head_sha": sha,
            }
            return {"ok": True, "stage": "prepared", "path": PATH,
                    "candidate_tree": TREE, "head_sha": COMMIT}

    def reserve(candidate_run, pr_url, live, *, feedback_key):
        assert feedback_key == f"review:{suggestion.signal_digest}"
        assert ledger.validate_pr_identity(candidate_run, pr_url, live)
        ledger.reservations += 1
        row = {"round": 1, "run_id": candidate_run, "state": "reserved",
               "expected_sha": RUNTIME_OLD, "feedback_key": review_key}
        ledger.rounds.append(row)
        return dict(row)

    monkeypatch.setattr(hive.observability_ledger, "reserve_pr_feedback_round", reserve)
    hive.config = replace(hive.config, pr_reviewer_ids=frozenset({"1234"}))
    hive.pr_observer = Observer()
    hive.pr_review_reader = Reader()
    hive.pr_commenter = object()
    modifier = Modifier()
    hive.self_modifier = modifier

    return hive, ledger, modifier, preparer, snapshot, suggestion, request_id


def test_runtime_source_review_suggestion_prepares_one_pending_candidate(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, snapshot, _suggestion, request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )

    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {
        "status": "review_pending", "round": 1, "request_id": request_id,
    }
    assert ledger.rounds[0]["state"] == "reserved"
    assert modifier.calls == 1 and preparer.calls == 1 and ledger.reservations == 1
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {
        "status": "review_pending", "round": 1, "request_id": request_id,
        "resumed": True,
    }
    assert modifier.calls == 1 and ledger.reservations == 1


def test_runtime_approved_source_review_rechecks_signal_before_writer_push(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, snapshot, suggestion, request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )
    calls = []

    class Writer:
        async def publish(self, selected_id, _authorizations, verify):
            calls.append(selected_id)
            live = await verify(RUNTIME_BRANCH, RUNTIME_OLD)
            assert live["ok"] is True and live["head_sha"] == RUNTIME_OLD
            assert live["pr_id"] == 99 and live["author_id"] == 4
            assert ledger.finish_pr_feedback_round(
                RUNTIME_URL, 1, state="pushed", new_sha=RUNTIME_NEW,
            )
            return {"ok": True, "stage": "pushed", "head_sha": RUNTIME_NEW}

    hive.supervised_review_writer = Writer()
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {
        "status": "pushed", "round": 1, "head_sha": RUNTIME_NEW,
    }
    assert calls == [request_id]
    assert modifier.calls == preparer.calls == ledger.reservations == 1
    assert suggestion.signal_digest == "d" * 64


def test_runtime_changed_source_review_signal_cannot_publish_or_regenerate(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, snapshot, suggestion, request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )

    class ChangedReader:
        async def select(self, *_args):
            return ReviewSelection("ready", replace(suggestion, signal_digest="e" * 64))

    class Writer:
        async def publish(self, *_args):
            raise AssertionError("changed review signal must not reach the writer")

    hive.pr_review_reader = ChangedReader()
    hive.supervised_review_writer = Writer()
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {
        "status": "manual_inspection_required",
    }
    assert modifier.calls == preparer.calls == ledger.reservations == 1
    assert ledger.finished == []


@pytest.mark.parametrize("withdrawal", ["signal", "reviewer"])
def test_source_review_reservation_cannot_resume_as_ci_repair(
    review_runtime, monkeypatch, withdrawal,
):
    hive, ledger, modifier, preparer, snapshot, _suggestion, request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )
    if withdrawal == "signal":
        class WithdrawnReader:
            async def select(self, *_args):
                return ReviewSelection("none", None)

        hive.pr_review_reader = WithdrawnReader()
    else:
        hive.config = replace(hive.config, pr_reviewer_ids=frozenset())

    writer_calls = []

    class Writer:
        async def publish(self, *_args):
            writer_calls.append(True)
            raise AssertionError("review reservation must not reach the CI writer")

    hive.supervised_review_writer = Writer()
    failed_ci = dict(snapshot, ci_state="failed", status="checks_failed")
    assert asyncio.run(hive.react_to_failed_pr_ci(failed_ci)) == {
        "status": "manual_inspection_required",
    }
    assert writer_calls == []
    assert ledger.rounds[0]["state"] == "reserved" and ledger.finished == []
    assert modifier.calls == preparer.calls == ledger.reservations == 1


def test_runtime_source_review_without_supervised_components_never_edits_or_pushes(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, snapshot, _suggestion, _request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )
    hive.supervised_review_preparer = None
    hive.supervised_review_writer = None
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {"status": "failed"}
    assert modifier.calls == preparer.calls == 0
    assert ledger.finished == [(RUNTIME_URL, 1, "failed", "")]


def test_runtime_source_review_drift_at_writer_verification_fails_closed(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, snapshot, suggestion, request_id = (
        _configure_source_review(review_runtime, monkeypatch)
    )
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )

    class DriftingReader:
        calls = 0

        async def select(self, *_args):
            self.calls += 1
            selected = suggestion if self.calls == 1 else replace(
                suggestion, signal_digest="e" * 64,
            )
            return ReviewSelection("ready", selected)

    reader = DriftingReader()
    hive.pr_review_reader = reader

    class Writer:
        async def publish(self, selected_id, _authorizations, verify):
            assert selected_id == request_id
            assert await verify(RUNTIME_BRANCH, RUNTIME_OLD) == {"ok": False}
            return {"ok": False, "stage": "declined"}

    hive.supervised_review_writer = Writer()
    assert asyncio.run(hive.react_to_failed_pr_ci(snapshot)) == {
        "status": "manual_inspection_required",
    }
    assert reader.calls == 2
    assert modifier.calls == preparer.calls == 1
    assert ledger.finished == []


def test_runtime_approved_round_invokes_writer_once_with_fresh_pr(review_runtime, monkeypatch):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )
    calls = []

    class Writer:
        async def publish(self, selected_id, _authorizations, verify):
            calls.append(selected_id)
            live = await verify(RUNTIME_BRANCH, RUNTIME_OLD)
            assert live["ok"] is True and live["head_sha"] == RUNTIME_OLD
            assert live["pr_id"] == 99 and live["author_id"] == 4
            assert ledger.finish_pr_feedback_round(
                RUNTIME_URL, 1, state="pushed", new_sha=RUNTIME_NEW,
            )
            return {"ok": True, "stage": "pushed", "head_sha": RUNTIME_NEW}

    hive.supervised_review_writer = Writer()
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": "pushed", "round": 1, "head_sha": RUNTIME_NEW,
    }
    assert calls == [request_id]
    assert ledger.finished == [(RUNTIME_URL, 1, "pushed", RUNTIME_NEW)]
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def test_runtime_nonowner_exception_cannot_finalize_reserved_round(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )

    class BrokenWriter:
        async def publish(self, _selected_id, _authorizations, _verify):
            raise OSError("untrusted transport details")

    hive.supervised_review_writer = BrokenWriter()
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": "manual_inspection_required",
    }
    assert ledger.finished == []
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def test_runtime_consumed_decision_finalizes_uncertain_round(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {
            "request_id": request_id, "state": "consumed",
            "owner_host": socket.gethostname(), "owner_pid": 1234,
        },
    )
    monkeypatch.setattr("hive.runtime.process_is_alive", lambda _pid: False)
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": "uncertain",
    }
    assert ledger.finished == [(RUNTIME_URL, 1, "uncertain", "")]
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": "stand_down_required",
    }
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def test_runtime_nonowner_cancellation_cannot_finalize_reserved_round(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": "approved"},
    )

    class InterruptedWriter:
        async def publish(self, _selected_id, _authorizations, _verify):
            raise asyncio.CancelledError

    hive.supervised_review_writer = InterruptedWriter()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))
    assert ledger.finished == []
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def test_concurrent_runtime_does_not_finalize_live_publication(
    review_runtime, monkeypatch,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    decision = {"state": "approved"}
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {
            "request_id": request_id, "state": decision["state"],
            "owner_host": socket.gethostname(), "owner_pid": os.getpid(),
        },
    )

    async def exercise():
        consumed = asyncio.Event()
        finish = asyncio.Event()

        class SlowWriter:
            async def publish(self, _selected_id, _authorizations, _verify):
                decision["state"] = "consumed"
                consumed.set()
                await finish.wait()
                assert ledger.finish_pr_feedback_round(
                    RUNTIME_URL, 1, state="pushed", new_sha=RUNTIME_NEW,
                )
                return {"ok": True, "stage": "pushed", "head_sha": RUNTIME_NEW}

        hive.supervised_review_writer = SlowWriter()
        first = asyncio.create_task(hive.react_to_failed_pr_ci(runtime_snapshot()))
        await consumed.wait()
        assert await hive.react_to_failed_pr_ci(runtime_snapshot()) == {
            "status": "publication_in_progress",
        }
        assert ledger.finished == []
        finish.set()
        assert await first == {
            "status": "pushed", "round": 1, "head_sha": RUNTIME_NEW,
        }

    asyncio.run(exercise())
    assert ledger.finished == [(RUNTIME_URL, 1, "pushed", RUNTIME_NEW)]
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


def test_consumed_decision_persists_publication_owner(tmp_path):
    _repo, db, store, link = _approved_candidate(tmp_path)
    approved = store.approved_supervised_candidate(link.request_id)
    assert approved is not None
    assert store.consume_supervised(approved)
    reopened = PrReviewAuthorizationStore(db)
    assert reopened.supervised_round_status(
        pr_url=link.binding.pr_url, expected_head=link.binding.expected_head,
        feedback_round=link.binding.feedback_round, run_id=link.binding.run_id,
    ) == {
        "request_id": link.request_id, "state": "consumed",
        "owner_host": socket.gethostname(), "owner_pid": os.getpid(),
    }


@pytest.mark.parametrize("decision", ["denied", "expired", "revoked"])
def test_runtime_terminal_review_decision_stands_down_without_push(
    review_runtime, monkeypatch, decision,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: {"request_id": request_id, "state": decision},
    )
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": "stand_down_required",
    }
    assert ledger.finished == [(RUNTIME_URL, 1, "failed", "")]
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)


@pytest.mark.parametrize("state", [None, "consumed"])
def test_runtime_unlinked_or_consumed_round_never_retries_generation(
    review_runtime, monkeypatch, state,
):
    hive, ledger, modifier, preparer, generator_calls, request_id = review_runtime
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot()))["status"] == "review_pending"
    selected = None if state is None else {"request_id": request_id, "state": state}
    monkeypatch.setattr(
        hive.pr_review_authorizations, "supervised_round_status",
        lambda **_kwargs: selected,
    )
    expected = "manual_inspection_required"
    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": expected,
    }
    assert ledger.finished == []
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)

    assert asyncio.run(hive.react_to_failed_pr_ci(runtime_snapshot())) == {
        "status": expected,
    }
    assert ledger.reservations == 1
    assert (modifier.calls, modifier.pushes, preparer.calls, len(generator_calls)) == (1, 0, 1, 1)
