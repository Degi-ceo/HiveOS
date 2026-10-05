"""M36 receipts bind fixed container diagnostics to one detached Git candidate."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
import uuid

import pytest

from hive.core.candidate_evidence import CandidateEvidenceBinding, CandidateEvidenceIssuer


BASE = "1" * 40
COMMIT = "2" * 40
TREE = "3" * 40
IMAGE = "sha256:" + "4" * 64


def _binding() -> CandidateEvidenceBinding:
    return CandidateEvidenceBinding(
        run_id=str(uuid.uuid4()), checkout_id="5" * 64, base_commit=BASE,
        candidate_commit=COMMIT, candidate_tree=TREE,
        candidate_digest=hashlib.sha256(f"git-tree\0{TREE}".encode()).hexdigest(),
        image_digest=IMAGE,
    )


class _Git:
    def __init__(self) -> None:
        self.head, self.parent, self.tree, self.status, self.detached = COMMIT, BASE, TREE, "", True

    async def __call__(self, command, _worktree):
        key = tuple(command)
        values = {
            ("git", "rev-parse", "HEAD"): self.head,
            ("git", "rev-parse", "HEAD^"): self.parent,
            ("git", "rev-parse", "HEAD^{tree}"): self.tree,
            ("git", "status", "--porcelain", "--ignored"): self.status,
        }
        if key == ("git", "symbolic-ref", "--quiet", "HEAD"):
            return (1 if self.detached else 0), ""
        return (0, values[key] + "\n")


class _Runner:
    pinned_image_digest = IMAGE

    def __init__(self, *, rc: int = 0, mutate: _Git | None = None) -> None:
        self.rc, self.mutate, self.calls = rc, mutate, []

    async def run_pinned_evidence(self, _worktree, argv):
        self.calls.append(argv)
        if self.mutate is not None and len(self.calls) == 1:
            self.mutate.tree = "f" * 40
        return self.rc, "private output api_key=must-not-persist"


def test_issuer_persists_restart_safe_redacted_receipt(tmp_path):
    git, runner, binding = _Git(), _Runner(), _binding()
    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", runner, git_run=git)

    receipt = asyncio.run(issuer.issue(binding, str(tmp_path)))

    assert receipt is not None
    assert receipt.check_results == (("ruff", 0), ("compileall", 0), ("pytest", 0))
    restored = CandidateEvidenceIssuer(tmp_path / "state.sqlite", runner, git_run=git).receipt(binding)
    assert restored == receipt
    serialized = str(restored)
    assert "private output" not in serialized and "api_key" not in serialized
    assert runner.calls == [
        ("ruff", "check", "src/hive"),
        ("python", "-m", "compileall", "src/hive"),
        ("python", "-m", "pytest", "-q", "tests"),
    ]


def test_issuer_refuses_dirty_attached_or_mismatched_checkout_without_diagnostics(tmp_path):
    for mutate in (
        lambda git: setattr(git, "status", " M src/hive/private.py"),
        lambda git: setattr(git, "status", "!! private-test-override.py"),
        lambda git: setattr(git, "detached", False),
        lambda git: setattr(git, "head", "f" * 40),
    ):
        git, runner, binding = _Git(), _Runner(), _binding()
        mutate(git)
        issuer = CandidateEvidenceIssuer(tmp_path / str(uuid.uuid4()), runner, git_run=git)
        assert asyncio.run(issuer.issue(binding, str(tmp_path))) is None
        assert not runner.calls and issuer.receipt(binding) is None


def test_issuer_rechecks_git_identity_after_diagnostics(tmp_path):
    git, binding = _Git(), _binding()
    runner = _Runner(mutate=git)
    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", runner, git_run=git)

    assert asyncio.run(issuer.issue(binding, str(tmp_path))) is None
    assert len(runner.calls) == 3
    assert issuer.receipt(binding) is None


def test_issuer_refuses_nonzero_diagnostic_and_pinned_image_mismatch(tmp_path):
    git, binding = _Git(), _binding()
    failed = _Runner(rc=1)
    issuer = CandidateEvidenceIssuer(tmp_path / "failed.sqlite", failed, git_run=git)
    assert asyncio.run(issuer.issue(binding, str(tmp_path))) is None
    assert len(failed.calls) == 1 and issuer.receipt(binding) is None

    wrong = _Runner()
    wrong.pinned_image_digest = "sha256:" + "f" * 64
    mismatch = CandidateEvidenceIssuer(tmp_path / "mismatch.sqlite", wrong, git_run=git)
    assert asyncio.run(mismatch.issue(binding, str(tmp_path))) is None
    assert not wrong.calls


def test_issuer_reraises_cancellation_without_receipt(tmp_path):
    class _Cancelled(_Runner):
        async def run_pinned_evidence(self, _worktree, _argv):
            raise asyncio.CancelledError()

    git, binding = _Git(), _binding()
    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", _Cancelled(), git_run=git)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(issuer.issue(binding, str(tmp_path)))
    assert issuer.receipt(binding) is None


def test_tampered_or_conflicting_receipt_is_never_returned(tmp_path):
    git, runner, binding = _Git(), _Runner(), _binding()
    db = tmp_path / "state.sqlite"
    issuer = CandidateEvidenceIssuer(db, runner, git_run=git)
    assert asyncio.run(issuer.issue(binding, str(tmp_path))) is not None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE candidate_evidence_receipts SET check_results_json=?",
            ('[["pytest",0]]',),
        )
    assert issuer.receipt(binding) is None


def test_existing_receipt_still_requires_a_matching_clean_checkout(tmp_path):
    git, runner, binding = _Git(), _Runner(), _binding()
    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", runner, git_run=git)
    assert asyncio.run(issuer.issue(binding, str(tmp_path))) is not None
    calls_before = len(runner.calls)
    git.status = "!! private-test-override.py"

    assert asyncio.run(issuer.issue(binding, str(tmp_path))) is None
    assert len(runner.calls) == calls_before


def test_binding_rejects_noncanonical_or_unpinned_identity():
    with pytest.raises(ValueError):
        CandidateEvidenceBinding(
            run_id="not-a-uuid", checkout_id="5" * 64, base_commit=BASE,
            candidate_commit=COMMIT, candidate_tree=TREE, candidate_digest="0" * 64,
            image_digest="python:3.12",
        )


def test_issuer_checks_a_real_detached_git_checkout(tmp_path):
    repo = tmp_path / "candidate-repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "hive@example.invalid"],
        ["git", "config", "user.name", "Hive Evidence Test"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)
    source = repo / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True, text=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "candidate", "-q"], cwd=repo,
                   check=True, capture_output=True, text=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", "-q", commit], cwd=repo,
                   check=True, capture_output=True, text=True)
    binding = CandidateEvidenceBinding(
        run_id=str(uuid.uuid4()), checkout_id="6" * 64, base_commit=base,
        candidate_commit=commit, candidate_tree=tree,
        candidate_digest=hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest(),
        image_digest=IMAGE,
    )

    async def real_git(command, worktree):
        completed = await asyncio.to_thread(
            subprocess.run, command, cwd=worktree, check=False, capture_output=True, text=True,
        )
        return completed.returncode, completed.stdout

    issuer = CandidateEvidenceIssuer(tmp_path / "state.sqlite", _Runner(), git_run=real_git)
    receipt = asyncio.run(issuer.issue(binding, str(repo)))

    assert receipt is not None
    assert issuer.receipt(binding) == receipt
