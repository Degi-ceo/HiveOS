"""Bounded, read-only view of one host-evidenced PR repair candidate."""

from __future__ import annotations

from pathlib import Path

from hive.core.pr_review_auth import PrReviewAuthorizationStore
from hive.core.redact import redact_known_secrets
from hive.core.secret_scan import scan_added_diff
from hive.core.supervised_review_prepare import RetainedReviewCandidateVerifier, _GitRun

_MAX_DIFF_BYTES = 8192
_MAX_DIFF_LINES = 120


def _scan_displayed_diff(diff: str) -> bool:
    """Find likely secrets on every line the approver would see, not just additions."""
    exposed: list[str] = []
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        if in_hunk and line.startswith(("+", "-", " ")):
            exposed.append("+" + line[1:])
        else:
            exposed.append(line)
    return bool(scan_added_diff("\n".join(exposed)))


class SupervisedReviewPreview(RetainedReviewCandidateVerifier):
    """Show only the exact retained object to an already-authenticated approver.

    The gateway owns authentication. This class neither approves a request
    nor calls a model, executes a candidate, contacts GitHub, or pushes.
    """

    def __init__(
        self, *, repo_root: str | Path, state_db: str | Path, git_run: _GitRun,
    ) -> None:
        super().__init__(repo_root=repo_root, git_run=git_run)
        self._state_db = Path(state_db).resolve()

    async def render(
        self, request_id: str, authorizations: PrReviewAuthorizationStore,
    ) -> dict[str, str] | None:
        if (
            not isinstance(authorizations, PrReviewAuthorizationStore)
            or authorizations.database_path.resolve() != self._state_db
        ):
            return None
        try:
            link = authorizations.supervised_candidate_for_preview(request_id)
            if link is None:
                return None
            binding = link.binding
            if (
                await self._verify_retained_candidate(binding) != link.candidate_commit
                or not self._has_policy_attestation(
                    authorizations, binding, link.candidate_commit,
                )
            ):
                return None
            rc, diff = await self._run(
                [
                    "git", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                    "--text", "--unified=3", binding.expected_head,
                    link.candidate_commit, "--", binding.path,
                ],
                self._root,
            )
            if (
                rc != 0 or not diff or "\x00" in diff
                or len(diff.encode("utf-8", errors="replace")) > _MAX_DIFF_BYTES
                or len(diff.splitlines()) > _MAX_DIFF_LINES
                or redact_known_secrets(diff) != diff
                or _scan_displayed_diff(diff)
                or authorizations.supervised_candidate_for_preview(request_id) != link
                or await self._verify_retained_candidate(binding) != link.candidate_commit
            ):
                return None
            return {
                "request_id": link.request_id,
                "binding_digest": authorizations._binding_digest(binding),
                "pr_url": binding.pr_url,
                "head_sha": binding.expected_head,
                "candidate_commit": link.candidate_commit,
                "candidate_digest": binding.candidate_digest,
                "path": binding.path,
                "operation": binding.operation,
                "diff": diff,
            }
        except Exception:  # noqa: BLE001 - preview uncertainty fails closed
            return None
