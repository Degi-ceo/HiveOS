"""M41 host-owned evidence preparation for one retained review candidate."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import uuid
from pathlib import Path

from hive.core.pr_review_auth import PrReviewAuthorizationStore, PrReviewBinding
from hive.core.supervised_review_prepare import SupervisedReviewEvidencePreparer
from hive.observability.persistence import ObservabilityLedger


IMAGE = "sha256:" + "4" * 64
BRANCH = "hive/auto-" + "a" * 32
URL = "https://github.com/Degi-ceo/HiveOS/pull/123"
PATH = "src/hive/example.py"


class _Runner:
    pinned_image_digest = IMAGE

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def run_pinned_evidence(self, worktree: str, argv: tuple[str, ...]):
        self.calls.append((worktree, tuple(argv)))
        return 0, "secret-like candidate output is intentionally discarded"


class _DriftRunner(_Runner):
    def __init__(self, repo: Path, retained_ref: str, replacement: str) -> None:
        super().__init__()
        self._repo = repo
        self._retained_ref = retained_ref
        self._replacement = replacement

    async def run_pinned_evidence(self, worktree: str, argv: tuple[str, ...]):
        result = await super().run_pinned_evidence(worktree, argv)
        if len(self.calls) == 3:
            _git(self._repo, "update-ref", self._retained_ref, self._replacement)
        return result


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


async def _run(command: str | list[str], cwd: str | None):
    completed = await asyncio.to_thread(
        subprocess.run, command, cwd=cwd, check=False, capture_output=True, text=True,
    )
    return completed.returncode, completed.stdout


def _binding(repo: Path, db: Path) -> tuple[PrReviewBinding, PrReviewAuthorizationStore]:
    base = _git(repo, "rev-parse", "HEAD^")
    candidate = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    run_id = str(uuid.uuid4())
    receipt = {
        "url": URL, "number": 123, "pr_id": 5001, "author_id": 7001,
        "head_repo_id": 9001, "base_repo_id": 9001, "head_ref": BRANCH,
        "head_sha": base, "base_ref": "main",
    }
    observation = {
        "number": 123, "url": URL, "state": "open", "head_sha": base,
        "pr_id": 5001, "author_id": 7001, "head_repo_id": 9001,
        "base_repo_id": 9001, "head_ref": BRANCH, "base_ref": "main",
    }
    ledger = ObservabilityLedger(db)
    try:
        ledger.record_selfmod({
            "run_id": run_id, "title": "candidate", "branch": BRANCH,
            "pr_url": URL, "head_sha": base, "stage": "pushed", "ok": True,
            "pr_creation": receipt,
        })
        assert ledger.bind_pr_identity(run_id, URL, observation)
        round_row = ledger.reserve_pr_feedback_round(
            run_id, URL, observation, feedback_key="review:deterministic-signal",
        )
        assert round_row is not None
    finally:
        ledger.close()
    binding = PrReviewBinding(
        owner="Degi-ceo", repo="HiveOS", pr_number=123, pr_url=URL,
        pr_id=5001, author_id=7001, head_repo_id=9001, base_repo_id=9001,
        branch=BRANCH, base_ref="main", expected_head=base, path=PATH,
        operation="PATCH_CODE", candidate_tree=tree,
        candidate_digest=hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest(),
        run_id=run_id, feedback_round=1, feedback_key_digest=round_row["feedback_key"],
    )
    return binding, PrReviewAuthorizationStore(db)


def test_supervisor_preparer_issues_evidence_then_atomically_exposes_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M41 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    binding, authorizations = _binding(repo, tmp_path / "state.sqlite")
    candidate = _git(repo, "rev-parse", "HEAD")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, candidate)
    assert authorizations.record_policy_checked_candidate(
        binding, candidate_commit=candidate, candidate_parent=binding.expected_head,
    )
    runner = _Runner()
    calls: list[tuple[str | list[str], str | None]] = []

    async def audited_run(command: str | list[str], cwd: str | None):
        calls.append((command, cwd))
        return await _run(command, cwd)

    async def verify(branch: str, head: str):
        assert (branch, head) == (BRANCH, binding.expected_head)
        return {"ok": True, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=tmp_path / "state.sqlite", runner=runner,
        git_run=audited_run,
    )
    link = asyncio.run(preparer.prepare(binding, authorizations, verify))

    assert link is not None
    assert link.candidate_commit == candidate
    assert link.binding == binding
    assert len(runner.calls) == 3
    assert authorizations.public_pending()[0]["id"] == link.request_id
    assert _git(repo, "rev-parse", retained_ref) == candidate
    assert not any(
        isinstance(command, list) and command[:2] == ["git", "push"]
        for command, _cwd in calls
    )


def test_supervisor_preparer_refuses_post_evidence_candidate_ref_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M41 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    binding, authorizations = _binding(repo, tmp_path / "state.sqlite")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", retained_ref, candidate)
    assert authorizations.record_policy_checked_candidate(
        binding, candidate_commit=candidate, candidate_parent=binding.expected_head,
    )
    runner = _DriftRunner(repo, retained_ref, binding.expected_head)

    async def verify(branch: str, head: str):
        return {"ok": True, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=tmp_path / "state.sqlite", runner=runner,
        git_run=_run,
    )

    assert asyncio.run(preparer.prepare(binding, authorizations, verify)) is None
    assert len(runner.calls) == 3
    assert authorizations.public_pending() == []


def test_supervisor_preparer_refuses_secret_bearing_retained_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M41 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text(
        "value = 2\napi_key = 'sk-test-token-12345678901234567890'\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-am", "candidate", "-q")
    binding, authorizations = _binding(repo, tmp_path / "state.sqlite")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, _git(repo, "rev-parse", "HEAD"))
    runner = _Runner()

    async def verify(branch: str, head: str):
        return {"ok": True, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=tmp_path / "state.sqlite", runner=runner,
        git_run=_run,
    )

    assert asyncio.run(preparer.prepare(binding, authorizations, verify)) is None
    assert runner.calls == []
    assert authorizations.public_pending() == []


def test_supervisor_preparer_refuses_retained_ref_without_policy_attestation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M41 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    binding, authorizations = _binding(repo, tmp_path / "state.sqlite")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, _git(repo, "rev-parse", "HEAD"))
    runner = _Runner()

    async def verify(branch: str, head: str):
        return {"ok": True, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=tmp_path / "state.sqlite", runner=runner,
        git_run=_run,
    )

    assert asyncio.run(preparer.prepare(binding, authorizations, verify)) is None
    assert runner.calls == []
    assert authorizations.public_pending() == []


def test_supervisor_preparer_fails_closed_when_policy_evidence_cannot_be_read(tmp_path):
    class UnreadablePolicyStore(PrReviewAuthorizationStore):
        def policy_checked_candidate(self, *args, **kwargs):
            raise OSError("state database unavailable")

    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M41 Test"),
    ):
        _git(repo, *command)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    state_db = tmp_path / "state.sqlite"
    binding, policy_store = _binding(repo, state_db)
    candidate = _git(repo, "rev-parse", "HEAD")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, candidate)
    assert policy_store.record_policy_checked_candidate(
        binding, candidate_commit=candidate, candidate_parent=binding.expected_head,
    )
    authorizations = UnreadablePolicyStore(state_db)
    runner = _Runner()

    async def verify(branch: str, head: str):
        return {"ok": True, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=state_db, runner=runner, git_run=_run,
    )

    assert asyncio.run(preparer.prepare(binding, authorizations, verify)) is None
    assert runner.calls == []
    assert policy_store.public_pending() == []
