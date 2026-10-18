"""Host-owned one-shot publication of an approved, supervisor-attested PR repair.

This host-owned writer cannot decide an approval or create/edit a candidate. It
reads exact durable M36/M39/M41 evidence, revalidates local Git and caller-
owned live PR identity, consumes one approver decision, then attempts one
exact-head lease-guarded fast-forward to the already-bound Hive PR branch.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from hive.core.pr_review_auth import (
    ApprovedSupervisedCandidate,
    PrReviewAuthorizationStore,
    PrReviewBinding,
)
from hive.core.supervised_review_prepare import (
    RetainedReviewCandidateVerifier,
    _GitRun,
)
from hive.observability.persistence import ObservabilityLedger

_FreshPrCheck = Callable[[str, str], Awaitable[dict[str, Any]]]
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SupervisedReviewWriter(RetainedReviewCandidateVerifier):
    """Perform at most one push attempt for one exact approved M41 candidate."""

    def __init__(
        self, *, repo_root: str | Path, state_db: str | Path,
        pinned_image_digest: str, git_run: _GitRun,
    ) -> None:
        super().__init__(repo_root=repo_root, git_run=git_run)
        if _IMAGE_DIGEST.fullmatch(pinned_image_digest) is None:
            raise ValueError("supervised writer requires a pinned evidence image")
        self._state_db = Path(state_db).resolve()
        self._pinned_image_digest = pinned_image_digest

    async def _fresh(
        self, binding: PrReviewBinding, expected_sha: str,
        verify_fresh_pr: _FreshPrCheck,
    ) -> bool:
        """Require an authenticated current GET matching durable creation identity."""
        try:
            observed = await verify_fresh_pr(binding.branch, expected_sha)
            if not isinstance(observed, dict) or observed.get("ok") is not True:
                return False
            exact = {
                "number": binding.pr_number,
                "url": binding.pr_url,
                "state": "open",
                "head_sha": expected_sha,
                "head_ref": binding.branch,
                "base_ref": binding.base_ref,
                "pr_id": binding.pr_id,
                "author_id": binding.author_id,
                "head_repo_id": binding.head_repo_id,
                "base_repo_id": binding.base_repo_id,
            }
            if any(observed.get(key) != value for key, value in exact.items()):
                return False
            ledger = ObservabilityLedger(self._state_db)
            try:
                return ledger.validate_pr_identity(
                    binding.run_id, binding.pr_url, observed,
                )
            finally:
                ledger.close()
        except Exception:  # noqa: BLE001 - live or storage uncertainty fails closed
            return False

    async def _remote_head(self, branch: str) -> str | None:
        ref = f"refs/heads/{branch}"
        try:
            rc, output = await self._run(
                ["git", "ls-remote", "--exit-code", "origin", ref], self._root,
            )
        except Exception:  # noqa: BLE001 - transport uncertainty fails closed
            return None
        lines = output.splitlines()
        if rc != 0 or len(lines) != 1:
            return None
        parts = lines[0].split("\t")
        if len(parts) != 2 or parts[1] != ref or _OID.fullmatch(parts[0]) is None:
            return None
        return parts[0]

    async def _trusted_origin(self, binding: PrReviewBinding) -> bool:
        """Reject a redirected fetch or push destination before network I/O."""
        repository = f"{binding.owner}/{binding.repo}"
        allowed = {
            f"https://github.com/{repository}",
            f"https://github.com/{repository}.git",
            f"git@github.com:{repository}",
            f"git@github.com:{repository}.git",
            f"ssh://git@github.com/{repository}",
            f"ssh://git@github.com/{repository}.git",
        }
        try:
            fetch_rc, fetch_url = await self._run(
                ["git", "remote", "get-url", "--all", "origin"], self._root,
            )
            push_rc, push_url = await self._run(
                ["git", "remote", "get-url", "--push", "--all", "origin"], self._root,
            )
        except Exception:  # noqa: BLE001 - remote uncertainty fails closed
            return False
        fetch_urls = fetch_url.splitlines()
        push_urls = push_url.splitlines()
        return bool(
            fetch_rc == push_rc == 0
            and len(fetch_urls) == len(push_urls) == 1
            and fetch_urls[0] in allowed
            and push_urls[0] in allowed
        )

    async def _candidate_is_current(
        self, approved: ApprovedSupervisedCandidate,
        authorizations: PrReviewAuthorizationStore,
    ) -> bool:
        try:
            return bool(
                await self._verify_retained_candidate(approved.binding)
                == approved.candidate_commit
                and self._has_policy_attestation(
                    authorizations, approved.binding, approved.candidate_commit,
                )
            )
        except Exception:  # noqa: BLE001 - local object uncertainty fails closed
            return False

    def _uncertain(self, binding: PrReviewBinding) -> dict[str, str | bool]:
        """Keep an ambiguous or interrupted one-shot round terminal."""
        try:
            ledger = ObservabilityLedger(self._state_db)
            try:
                ledger.finish_pr_feedback_round(
                    binding.pr_url, binding.feedback_round, state="uncertain",
                )
            finally:
                ledger.close()
        except Exception:  # noqa: BLE001 - consumed approval still forbids retry
            pass
        return {"ok": False, "stage": "push_uncertain"}

    async def publish(
        self, request_id: str, authorizations: PrReviewAuthorizationStore,
        verify_fresh_pr: _FreshPrCheck,
    ) -> dict[str, str | bool]:
        """Spend one decision immediately before an exact-head fast-forward.

        After consumption, all failures are terminal/uncertain. A caller must
        not retry an ambiguous transport result. This seam does not post PR
        comments, merge, or activate autonomous source repair.
        """
        declined: dict[str, str | bool] = {"ok": False, "stage": "declined"}
        if (
            not isinstance(authorizations, PrReviewAuthorizationStore)
            or not callable(verify_fresh_pr)
            or authorizations.database_path.resolve() != self._state_db
        ):
            return declined
        try:
            approved = authorizations.approved_supervised_candidate(request_id)
        except Exception:  # noqa: BLE001 - storage uncertainty is never authority
            return declined
        if approved is None:
            return declined
        if approved.evidence_binding.image_digest != self._pinned_image_digest:
            return declined
        binding = approved.binding
        if (
            not await self._trusted_origin(binding)
            or not await self._fresh(binding, binding.expected_head, verify_fresh_pr)
            or await self._remote_head(binding.branch) != binding.expected_head
            or not await self._candidate_is_current(approved, authorizations)
        ):
            return declined
        try:
            still_approved = authorizations.approved_supervised_candidate(request_id)
        except Exception:  # noqa: BLE001 - storage uncertainty is never authority
            return declined
        if (
            still_approved != approved
            or not await self._trusted_origin(binding)
            or not await self._fresh(binding, binding.expected_head, verify_fresh_pr)
            or await self._remote_head(binding.branch) != binding.expected_head
            or not await self._candidate_is_current(approved, authorizations)
        ):
            return declined
        try:
            if not authorizations.consume_supervised(approved):
                return declined
        except Exception:  # noqa: BLE001 - uncertain consumption forbids push
            return declined
        try:
            return await self._publish_consumed(approved, binding)
        except asyncio.CancelledError:
            self._uncertain(binding)
            raise
        except Exception:  # noqa: BLE001 - consumed approval is terminal
            return self._uncertain(binding)

    async def _publish_consumed(
        self, approved: ApprovedSupervisedCandidate, binding: PrReviewBinding,
    ) -> dict[str, str | bool]:
        """Only the writer that consumed the decision can settle its failed attempt."""
        # A consumed decision is terminal even if a remote operation fails.
        if (
            not await self._trusted_origin(binding)
            or await self._remote_head(binding.branch) != binding.expected_head
        ):
            return self._uncertain(binding)
        try:
            push_rc, _ = await self._run(
                [
                    "git", "-c", "core.hooksPath=/dev/null",
                    "-c", "remote.origin.mirror=false",
                    "-c", "push.followTags=false",
                    "push", "--porcelain", "--no-follow-tags",
                    f"--force-with-lease=refs/heads/{binding.branch}:{binding.expected_head}",
                    "origin",
                    f"{approved.candidate_commit}:refs/heads/{binding.branch}",
                ],
                self._root,
            )
        except Exception:  # noqa: BLE001 - ambiguous network result is terminal
            return self._uncertain(binding)
        if push_rc != 0 or await self._remote_head(binding.branch) != approved.candidate_commit:
            return self._uncertain(binding)
        try:
            ledger = ObservabilityLedger(self._state_db)
            try:
                finished = ledger.finish_pr_feedback_round(
                    binding.pr_url, binding.feedback_round,
                    state="pushed", new_sha=approved.candidate_commit,
                )
            finally:
                ledger.close()
        except Exception:  # noqa: BLE001 - remote may have moved; never retry
            return self._uncertain(binding)
        if not finished:
            return self._uncertain(binding)
        return {
            "ok": True, "stage": "pushed", "head_sha": approved.candidate_commit,
        }
