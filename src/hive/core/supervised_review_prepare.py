"""Host-owned M41 evidence preparation for a retained REVIEW candidate.

This dormant seam cannot create a candidate, decide or consume an approval,
push, merge, or call a model. It only turns an already-retained exact Git
object into M36 evidence and an M40 pending request after repeated local and
caller-owned live-PR validation.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from hive.core.candidate_evidence import (
    CandidateEvidenceBinding,
    CandidateEvidenceIssuer,
    PinnedEvidenceRunner,
)
from hive.core.pr_review_auth import (
    PrReviewAuthorizationStore,
    PrReviewBinding,
    PrReviewEvidenceLink,
)
from hive.core.secret_scan import scan_added_diff, scan_candidate_paths

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FreshPrCheck = Callable[[str, str], Awaitable[dict]]
_GitRun = Callable[[str | list[str], str | None], Awaitable[tuple[int, str]]]
_EVALUATION_CONTROL_PREFIXES = (
    "evals/",
    "src/hive/evals/",
    "src/hive/core/learning/",
)
_EVALUATION_CONTROL_EXACT = {
    "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py",
    "sitecustomize.py", "usercustomize.py", "src/sitecustomize.py",
    "src/usercustomize.py", "src/hive/__init__.py",
}


def _candidate_ref(binding: PrReviewBinding) -> str:
    digest = hashlib.sha256(binding.canonical_json().encode("utf-8")).hexdigest()
    return f"refs/hive/pr-review-candidates/{digest}"


def _is_evaluation_control(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    return (
        normalized in _EVALUATION_CONTROL_EXACT
        or normalized.endswith(("/conftest.py", "/sitecustomize.py", "/usercustomize.py"))
        or any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in _EVALUATION_CONTROL_PREFIXES
        )
    )


class SupervisedReviewEvidencePreparer:
    """Attach host-issued evidence to one retained candidate without publishing it."""

    def __init__(
        self, *, repo_root: str | Path, state_db: str | Path,
        runner: PinnedEvidenceRunner, git_run: _GitRun,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._state_db = Path(state_db).resolve()
        self._runner = runner
        self._git_run = git_run

    async def _run(self, command: list[str], cwd: str | Path | None = None) -> tuple[int, str]:
        return await self._git_run(command, str(cwd) if cwd is not None else None)

    async def _fresh(self, binding: PrReviewBinding, verify_fresh_pr: _FreshPrCheck) -> bool:
        try:
            value = await verify_fresh_pr(binding.branch, binding.expected_head)
        except Exception:  # noqa: BLE001 - caller-owned live evidence fails closed
            return False
        return bool(
            isinstance(value, dict)
            and value.get("ok") is True
            and value.get("branch") == binding.branch
            and value.get("head_sha") == binding.expected_head
        )

    async def _verify_retained_candidate(self, binding: PrReviewBinding) -> str | None:
        """Require the retained object to remain the one reviewed by M40."""
        if _is_evaluation_control(binding.path):
            return None
        ref = _candidate_ref(binding)
        exists_rc, _ = await self._run(
            ["git", "show-ref", "--verify", "--quiet", ref], self._root,
        )
        if exists_rc != 0:
            return None
        commit_rc, candidate_commit = await self._run(["git", "rev-parse", ref], self._root)
        candidate_commit = candidate_commit.strip().lower()
        if commit_rc != 0 or _OID.fullmatch(candidate_commit) is None:
            return None
        parent_rc, parent = await self._run(
            ["git", "rev-parse", f"{candidate_commit}^"], self._root,
        )
        tree_rc, tree = await self._run(
            ["git", "rev-parse", f"{candidate_commit}^{{tree}}"], self._root,
        )
        lineage_rc, lineage = await self._run(
            ["git", "rev-list", "--parents", "-n", "1", candidate_commit], self._root,
        )
        if (
            parent_rc != 0
            or parent.strip().lower() != binding.expected_head
            or tree_rc != 0
            or tree.strip().lower() != binding.candidate_tree
            or lineage_rc != 0
            or lineage.strip().lower().split() != [candidate_commit, binding.expected_head]
        ):
            return None
        diff_rc, diff = await self._run(
            ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", candidate_commit],
            self._root,
        )
        if diff_rc != 0 or [line for line in diff.splitlines() if line] != [f"M\t{binding.path}"]:
            return None
        mode_rc, mode = await self._run(
            ["git", "ls-tree", candidate_commit, "--", binding.path], self._root,
        )
        if mode_rc != 0 or not mode.startswith("100644 blob "):
            return None
        scan_rc, added_diff = await self._run(
            [
                "git", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--text",
                "--unified=0", binding.expected_head, candidate_commit, "--", ".",
            ],
            self._root,
        )
        if scan_rc != 0:
            return None
        try:
            if scan_added_diff(added_diff) or scan_candidate_paths([binding.path]):
                return None
        except Exception:  # noqa: BLE001 - scanner uncertainty fails closed
            return None
        return candidate_commit

    @staticmethod
    def _has_policy_attestation(
        authorizations: PrReviewAuthorizationStore, binding: PrReviewBinding,
        candidate_commit: str,
    ) -> bool:
        """Fail closed if the durable M35 post-gate proof is unavailable."""
        try:
            return authorizations.policy_checked_candidate(binding) == {
                "candidate_commit": candidate_commit,
                "candidate_parent": binding.expected_head,
            }
        except Exception:  # noqa: BLE001 - storage uncertainty is never approval
            return False

    async def prepare(
        self, binding: PrReviewBinding, authorizations: PrReviewAuthorizationStore,
        verify_fresh_pr: _FreshPrCheck,
    ) -> PrReviewEvidenceLink | None:
        """Issue M36 evidence, then atomically create the M40 review handoff.

        The evidence checkout is detached and removed before final local and
        live-PR revalidation. A receipt can remain after a later mismatch, but
        no pending authorization is exposed unless the retained object is still
        valid at M40 commit time.
        """
        if (
            not isinstance(binding, PrReviewBinding)
            or not isinstance(authorizations, PrReviewAuthorizationStore)
            or not callable(verify_fresh_pr)
            or authorizations.database_path.resolve() != self._state_db
        ):
            return None
        if not await self._fresh(binding, verify_fresh_pr):
            return None
        candidate_commit = await self._verify_retained_candidate(binding)
        if candidate_commit is None:
            return None
        if not self._has_policy_attestation(authorizations, binding, candidate_commit):
            return None
        try:
            evidence_binding = CandidateEvidenceBinding(
                run_id=binding.run_id,
                checkout_id=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                base_commit=binding.expected_head,
                candidate_commit=candidate_commit,
                candidate_tree=binding.candidate_tree,
                candidate_digest=binding.candidate_digest,
                image_digest=self._runner.pinned_image_digest,
            )
        except (AttributeError, TypeError, ValueError):
            return None
        checkout_parent = self._root / ".worktrees"
        checkout_parent.mkdir(parents=True, exist_ok=True)
        checkout = checkout_parent / f"hive-supervisor-evidence-{uuid.uuid4().hex}"
        add_rc, _ = await self._run(
            ["git", "worktree", "add", "--detach", str(checkout), candidate_commit], self._root,
        )
        if add_rc != 0:
            return None
        cleanup_ok = False
        receipt = None
        try:
            try:
                issuer = CandidateEvidenceIssuer(
                    self._state_db, self._runner, git_run=self._git_run,
                )
                receipt = await issuer.issue(evidence_binding, str(checkout))
            except Exception:  # noqa: BLE001 - evidence uncertainty fails closed
                receipt = None
        finally:
            try:
                remove_rc, _ = await self._run(
                    ["git", "worktree", "remove", "--force", str(checkout)], self._root,
                )
                cleanup_ok = remove_rc == 0
            except Exception:  # noqa: BLE001 - cleanup uncertainty fails closed
                cleanup_ok = False
        if receipt is None or not cleanup_ok:
            return None
        if not await self._fresh(binding, verify_fresh_pr):
            return None
        if (
            await self._verify_retained_candidate(binding) != candidate_commit
            or not self._has_policy_attestation(authorizations, binding, candidate_commit)
        ):
            return None
        return authorizations.prepare_candidate_with_host_evidence(
            binding,
            candidate_commit=candidate_commit,
            candidate_parent=binding.expected_head,
            evidence_binding=evidence_binding,
        )
