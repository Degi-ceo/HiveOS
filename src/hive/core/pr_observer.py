"""Read-only GitHub pull-request observation for Hive self-modification.

This module has no mutation endpoint and deliberately exposes only safe review
metadata.  It cannot merge, push, modify branches, or post comments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True, slots=True)
class PRObservation:
    number: int
    url: str
    state: str
    head_sha: str
    status: str
    checks_total: int
    checks_failed: int
    checks_pending: int
    review_state: str
    changes_requested: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "url": self.url, "state": self.state,
            "head_sha": self.head_sha, "status": self.status,
            "checks_total": self.checks_total, "checks_failed": self.checks_failed,
            "checks_pending": self.checks_pending, "review_state": self.review_state,
            "changes_requested": self.changes_requested,
        }


Fetcher = Callable[[str], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_PENDING = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


def classify_pr(pr: dict[str, Any], checks: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> PRObservation:
    """Classify one PR without returning title, body, review text, or author data."""
    state = str(pr.get("state", "unknown")).casefold()
    draft = bool(pr.get("draft"))
    failed = sum(1 for item in checks if str(item.get("conclusion", "")).casefold() in _FAILED)
    pending = sum(1 for item in checks if str(item.get("status", "")).casefold() in _PENDING)
    # The reviews endpoint is historical.  Compute each reviewer's latest
    # effective state instead of treating an old request for changes as current.
    latest_reviews: dict[str, tuple[str, str, int]] = {}
    for index, item in enumerate(reviews):
        state_name = str(item.get("state", "")).casefold()
        reviewer = item.get("user") if isinstance(item.get("user"), dict) else {}
        reviewer_id = str(reviewer.get("id") or item.get("user_id") or f"anonymous-{index}")
        submitted = str(item.get("submitted_at") or "")
        ordering = (submitted, index)
        previous = latest_reviews.get(reviewer_id)
        if previous is None or ordering >= (previous[1], previous[2]):
            latest_reviews[reviewer_id] = (state_name, submitted, index)
    review_states = {state_name for state_name, _, _ in latest_reviews.values()}
    changes_requested = sum(state_name == "changes_requested" for state_name in review_states)
    if state != "open":
        status = "closed"
    elif draft:
        status = "draft"
    elif failed:
        status = "checks_failed"
    elif pending:
        status = "checks_pending"
    elif changes_requested:
        status = "changes_requested"
    elif "approved" in review_states:
        status = "ready_for_human_merge"
    else:
        status = "waiting_review"
    review_state = "changes_requested" if changes_requested else (
        "approved" if "approved" in review_states else "waiting"
    )
    return PRObservation(
        number=int(pr.get("number", 0) or 0), url=str(pr.get("html_url", "")),
        state=state, head_sha=str((pr.get("head") or {}).get("sha", "")), status=status,
        checks_total=len(checks), checks_failed=failed, checks_pending=pending,
        review_state=review_state, changes_requested=changes_requested,
    )


class GitHubPRObserver:
    """Fetch and classify GitHub PR state through GET-only requests."""

    def __init__(self, token: str, owner: str, repo: str, *, fetcher: Fetcher | None = None) -> None:
        self._token, self._owner, self._repo = token, owner, repo
        self._fetcher = fetcher

    @property
    def available(self) -> bool:
        return bool(self._token and self._owner and self._repo)

    async def _get(self, path: str) -> dict[str, Any] | list[dict[str, Any]]:
        if self._fetcher is not None:
            return await self._fetcher(path)
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"https://api.github.com{path}",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            return response.json()

    async def observe(self, number: int) -> PRObservation:
        """Fetch one PR plus checks and reviews; raises if GET-only access fails."""
        if not self.available:
            raise RuntimeError("GitHub PR observation is not configured")
        base = f"/repos/{self._owner}/{self._repo}/pulls/{int(number)}"
        pr = await self._get(base)
        assert isinstance(pr, dict)
        sha = str((pr.get("head") or {}).get("sha", ""))
        checks = await self._get(f"/repos/{self._owner}/{self._repo}/commits/{sha}/check-runs")
        reviews = await self._get(f"{base}/reviews")
        check_rows = list((checks.get("check_runs") or []) if isinstance(checks, dict) else [])
        review_rows = list(reviews if isinstance(reviews, list) else [])
        return classify_pr(pr, check_rows, review_rows)
