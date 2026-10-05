"""M35 prepares one review-bound code candidate without any remote write."""
from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from hive.core.pr_review_auth import PrReviewAuthorizationStore, PrReviewContext
from hive.core.self_mod import SelfModifier


BRANCH = "hive/auto-" + "a" * 32
HEAD = "1" * 40
COMMIT = "2" * 40
TREE = "3" * 40
PATH = "src/hive/repair.py"


class FakeGit:
    def __init__(self, *, changed: str = PATH, remote_head: str = HEAD,
                 change_status: str = "M") -> None:
        self.changed = changed
        self.remote_head = remote_head
        self.change_status = change_status
        self.committed = False
        self.pushes = 0
        self.tests = 0
        self.refs: dict[str, str] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    async def __call__(self, command, cwd=None):
        args = command if isinstance(command, list) else command.split()
        self.calls.append((args, cwd))
        if args[:2] == ["git", "ls-remote"]:
            return 0, f"{self.remote_head}\trefs/heads/{BRANCH}\n"
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return 0, HEAD + "\n"
        if args[:2] == ["git", "rev-parse"] and len(args) == 3:
            target = args[2]
            if target in self.refs:
                return 0, self.refs[target] + "\n"
            if target in {"HEAD", "HEAD^", f"{COMMIT}^"}:
                return 0, (COMMIT if target == "HEAD" and self.committed else HEAD) + "\n"
            if target in {"HEAD^{tree}", f"{COMMIT}^{{tree}}"}:
                return 0, TREE + "\n"
        if args[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, BRANCH + "\n"
        if args[:2] == ["git", "write-tree"]:
            return 0, TREE + "\n"
        if "commit-tree" in args:
            return 0, "4" * 40 + "\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return 0, self.changed + "\n"
        if args[:3] == ["git", "diff", "--cached"] and "--name-only" in args:
            return 0, self.changed + "\n"
        if args[:3] == ["git", "diff", "--cached"]:
            return 0, "+safe change\n"
        if args[:2] == ["git", "diff"] and HEAD + "^" in args:
            return 0, "+prior failing change\n"
        if args[:2] == ["git", "diff-tree"]:
            return 0, "".join(
                f"{self.change_status}\t{path}\n" for path in self.changed.splitlines()
            )
        if args[:2] == ["git", "ls-tree"]:
            return 0, f"100644 blob {'5' * 40}\t{args[-1]}\n"
        if args[:2] == ["git", "update-ref"]:
            self.refs[args[2]] = args[3]
            return 0, ""
        if args[:3] == ["git", "show-ref", "--verify"]:
            return (0, "") if args[-1] in self.refs else (1, "")
        if args[:2] == ["git", "ls-files"]:
            return 0, ""
        if args[:2] == ["git", "status"]:
            return 0, "M " + self.changed + "\n"
        if args[:2] == ["git", "commit"]:
            self.committed = True
            return 0, "ok"
        if args[:2] == ["git", "push"]:
            self.pushes += 1
            return 0, "unexpected push"
        if args and args[0] == "pytest":
            self.tests += 1
            return (1, "failing base") if self.tests == 1 else (0, "passed")
        return 0, "ok"


async def _gate(_worktree, _base, _paths, _run_id, digest):
    return {"ok": True, "candidate_digest": digest}


def _context(run_id: str) -> PrReviewContext:
    return PrReviewContext(
        owner="Degi-ceo", repo="HiveOS", pr_number=42,
        pr_url="https://github.com/Degi-ceo/HiveOS/pull/42",
        pr_id=42, author_id=7, head_repo_id=8, base_repo_id=8,
        branch=BRANCH, base_ref="main", expected_head=HEAD, run_id=run_id,
        feedback_round=1, feedback_key_digest=hashlib.sha256(b"ci:head").hexdigest(),
    )


def test_prepare_source_candidate_persists_ref_and_receipt_without_push(tmp_path):
    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")
    verified: list[tuple[str, str]] = []

    async def verify(branch, head):
        verified.append((branch, head))
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is True and result["stage"] == "prepared"
    assert result["head_sha"] == COMMIT
    assert verified == [(BRANCH, HEAD), (BRANCH, HEAD)]
    assert git.pushes == 0
    assert not any(cmd[:2] == ["git", "push"] for cmd, _ in git.calls)
    assert any(cmd[:2] == ["git", "update-ref"] for cmd, _ in git.calls)
    binding = context.bind_candidate(PATH, TREE)
    assert store.prepared_candidate(result["request_id"], binding) == {
        "candidate_commit": COMMIT,
        "candidate_parent": HEAD,
    }
    assert git.refs[result["candidate_ref"]] == COMMIT


def test_prepare_refuses_multiple_changed_files_before_retaining_or_receipting(tmp_path):
    git = FakeGit(changed="src/hive/one.py\nsrc/hive/two.py")
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return ["src/hive/one.py", "src/hive/two.py"]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is False and result["stage"] == "review_prepare"
    assert git.pushes == 0 and not git.refs
    assert store.public_pending() == []


def test_prepare_receipt_failure_never_pushes_or_creates_an_authorization(tmp_path):
    class BrokenStore(PrReviewAuthorizationStore):
        def prepare_candidate(self, *args, **kwargs):
            raise OSError("durable storage unavailable")

    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = BrokenStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is False and result["stage"] == "review_prepare"
    assert git.pushes == 0 and len(git.refs) == 1
    assert store.public_pending() == []


def test_prepare_receipt_read_uncertainty_never_pushes(tmp_path):
    class ReadFailStore(PrReviewAuthorizationStore):
        def prepared_candidate(self, *args, **kwargs):
            raise OSError("receipt read unavailable")

    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = ReadFailStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is False and result["stage"] == "review_prepare"
    assert git.pushes == 0 and len(git.refs) == 1
    assert len(store.public_pending()) == 1


def test_review_context_rejects_boolean_feedback_round():
    with pytest.raises(ValueError, match="repair round"):
        PrReviewContext(
            owner="Degi-ceo", repo="HiveOS", pr_number=42,
            pr_url="https://github.com/Degi-ceo/HiveOS/pull/42",
            pr_id=42, author_id=7, head_repo_id=8, base_repo_id=8,
            branch=BRANCH, base_ref="main", expected_head=HEAD,
            run_id=str(uuid.uuid4()), feedback_round=True,
            feedback_key_digest=hashlib.sha256(b"ci:head").hexdigest(),
        )


def test_prepare_refuses_new_source_file_before_retaining_or_receipting(tmp_path):
    git = FakeGit(change_status="A")
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is False and result["stage"] == "changed_files"
    assert git.pushes == 0 and not git.refs
    assert store.public_pending() == []


def test_prepare_refuses_to_overwrite_a_conflicting_candidate_ref(tmp_path):
    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")
    binding = context.bind_candidate(PATH, TREE)
    ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    git.refs[ref] = "9" * 40

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store, candidate_gate=_gate,
    ))

    assert result["ok"] is False and result["stage"] == "review_prepare"
    assert git.refs[ref] == "9" * 40 and git.pushes == 0
    assert store.public_pending() == []
