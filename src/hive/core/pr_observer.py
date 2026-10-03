"""Read-only GitHub pull-request observation for Hive self-modification.

This module has no mutation endpoint. It exposes only bounded, redacted review
evidence tagged untrusted, and cannot merge, push, modify branches, or comment.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hive.core.redact import redact_known_secrets


class PRRateLimited(RuntimeError):
    """A GET was rate-limited; only the retry deadline crosses this boundary."""

    def __init__(self, retry_at: float) -> None:
        super().__init__("GitHub PR observation is rate-limited")
        self.retry_at = retry_at


class PRPollDeferred(RuntimeError):
    """A durable per-PR or global cooldown already owns this observation."""


class PRNotTracked(ValueError):
    """The requested PR is not linked to a Hive self-modification run."""


def _retry_at(headers: Any, now: float) -> float:
    """Follow GitHub's retry-after/reset order, with a conservative fallback."""
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        try:
            seconds = float(retry_after)
            if math.isfinite(seconds) and seconds >= 0:
                return now + max(1.0, seconds)
        except (TypeError, ValueError):
            pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            reset = float(headers.get("x-ratelimit-reset", ""))
            if math.isfinite(reset) and reset > now:
                return reset
        except (TypeError, ValueError):
            pass
    return now + 60.0


def _safe_text(value: object, *, limit: int = 500) -> str:
    return redact_known_secrets(str(value or "").replace("\x00", ""))[:limit]


def _untrusted_text(value: object) -> dict[str, str]:
    return {"trust": "untrusted", "text": _safe_text(value)}


def _safe_id(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


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
    checks: tuple[dict[str, str], ...] = ()
    review_notes: tuple[dict[str, Any], ...] = ()
    pr_body: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "url": self.url, "state": self.state,
            "head_sha": self.head_sha, "status": self.status,
            "checks_total": self.checks_total, "checks_failed": self.checks_failed,
            "checks_pending": self.checks_pending, "review_state": self.review_state,
            "changes_requested": self.changes_requested,
            "checks": list(self.checks), "review_notes": list(self.review_notes),
            "pr_body": self.pr_body,
        }


Fetcher = Callable[[str], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_PENDING = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


def classify_pr(pr: dict[str, Any], checks: list[dict[str, Any]], reviews: list[dict[str, Any]],
                comments: list[dict[str, Any]] | None = None, *, incomplete: bool = False,
                checks_total: int | None = None) -> PRObservation:
    """Classify one PR; retain only bounded, redacted, untrusted review evidence."""
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
    review_states = [state_name for state_name, _, _ in latest_reviews.values()]
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
    if incomplete and state == "open" and not draft and status not in {
        "checks_failed", "checks_pending",
    }:
        status = "incomplete_evidence"
    review_state = "changes_requested" if changes_requested else (
        "approved" if "approved" in review_states else "waiting"
    )
    if incomplete:
        review_state = "incomplete"
    safe_checks = tuple({
        "name": _safe_text(item.get("name"), limit=120),
        "status": _safe_text(item.get("status"), limit=32),
        "conclusion": _safe_text(item.get("conclusion"), limit=32),
    } for item in checks[:30] if isinstance(item, dict))
    notes: list[dict[str, Any]] = []
    for kind, rows in (("review", reviews), ("inline", comments or [])):
        for item in rows:
            if not isinstance(item, dict) or not item.get("body"):
                continue
            notes.append({"kind": kind, "id": _safe_id(item.get("id")),
                          "body": _untrusted_text(item.get("body"))})
            if len(notes) >= 10:
                break
        if len(notes) >= 10:
            break
    return PRObservation(
        number=int(pr.get("number", 0) or 0), url=_safe_text(pr.get("html_url"), limit=300),
        state=state, head_sha=str((pr.get("head") or {}).get("sha", "")), status=status,
        checks_total=max(len(checks), checks_total or 0), checks_failed=failed, checks_pending=pending,
        review_state=review_state, changes_requested=changes_requested,
        checks=safe_checks, review_notes=tuple(notes),
        pr_body=_untrusted_text(pr.get("body")) if pr.get("body") else None,
    )


class GitHubPRObserver:
    """Fetch and classify GitHub PR state through GET-only requests."""

    def __init__(self, token: str, owner: str, repo: str, *, fetcher: Fetcher | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._token, self._owner, self._repo = token, owner, repo
        self._fetcher = fetcher
        self._clock = clock

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
            if response.status_code in {403, 429}:
                raise PRRateLimited(_retry_at(response.headers, self._clock()))
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
        checks = await self._get(f"/repos/{self._owner}/{self._repo}/commits/{sha}/check-runs?per_page=100")
        reviews = await self._get(f"{base}/reviews?per_page=100")
        comments = await self._get(f"{base}/comments?per_page=100")
        check_rows = list((checks.get("check_runs") or []) if isinstance(checks, dict) else [])
        review_rows = list(reviews if isinstance(reviews, list) else [])
        comment_rows = list(comments if isinstance(comments, list) else [])
        has_total = (isinstance(checks, dict)
                     and isinstance(checks.get("total_count"), int)
                     and checks["total_count"] >= 0)
        total = _safe_id(checks.get("total_count")) if has_total else 0
        incomplete = (total > len(check_rows) or (not has_total and len(check_rows) >= 100)
                      or len(review_rows) >= 100 or len(comment_rows) >= 100)
        return classify_pr(pr, check_rows, review_rows, comment_rows,
                           incomplete=incomplete, checks_total=total)
