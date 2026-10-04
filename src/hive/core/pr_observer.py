"""Read-only GitHub pull-request observation for Hive self-modification.

This module has no mutation endpoint. It exposes only bounded, redacted review
evidence tagged untrusted, and cannot merge, push, modify branches, or comment.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import unquote, unquote_plus

from hive.core.redact import known_secret_values, redact_known_secrets


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


_OMITTED_SECRET = "[omitted credential-bearing PR evidence]"
_OMITTED_OVERSIZED = "[omitted oversized PR evidence]"
_MAX_RAW_EVIDENCE = 8_192


def _secret_fragments(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({part.strip() for value in (*known_secret_values(), *values)
                         if isinstance(value, str)
                         for part in value.replace("\r", "\n").replace(",", "\n").split("\n")
                         if part.strip()}, key=len, reverse=True))


def _contains_secret(raw: str, fragments: tuple[str, ...]) -> bool:
    if not fragments:
        return False
    for decoder in (unquote, unquote_plus):
        layer = raw
        for _ in range(32):
            if any(fragment in layer for fragment in fragments):
                return True
            decoded = decoder(layer)
            if decoded == layer:
                break
            if len(decoded) >= len(layer):
                return any(fragment in decoded for fragment in fragments)
            layer = decoded
        else:
            return True  # More layers than the CPU budget: fail closed.
    return False


def _safe_text(value: object, *, limit: int = 500,
               secret_fragments: tuple[str, ...] = ()) -> str:
    raw = str(value or "").replace("\x00", "")
    if len(raw) > _MAX_RAW_EVIDENCE or len(raw.encode("utf-8")) > _MAX_RAW_EVIDENCE:
        return _OMITTED_OVERSIZED
    if _contains_secret(raw, secret_fragments or _secret_fragments(())):
        return _OMITTED_SECRET
    safe = redact_known_secrets(raw).encode("utf-8")
    return safe[:limit].decode("utf-8", errors="ignore")


def _untrusted_text(value: object, *, secret_fragments: tuple[str, ...] = ()) -> dict[str, str]:
    return {"trust": "untrusted", "text": _safe_text(
        value, secret_fragments=secret_fragments,
    )}


def _safe_id(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_sha(value: object) -> str:
    sha = str(value or "")
    return sha if 0 < len(sha) <= 64 and all(
        character in "0123456789abcdefABCDEF" for character in sha
    ) else ""


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
    pr_id: int = 0
    author_id: int = 0
    head_repo_id: int = 0
    head_ref: str = ""
    base_repo_id: int = 0
    base_ref: str = ""
    ci_state: str = "unknown"
    draft: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "url": self.url, "state": self.state,
            "head_sha": self.head_sha, "status": self.status,
            "checks_total": self.checks_total, "checks_failed": self.checks_failed,
            "checks_pending": self.checks_pending, "review_state": self.review_state,
            "changes_requested": self.changes_requested,
            "checks": list(self.checks), "review_notes": list(self.review_notes),
            "pr_body": self.pr_body,
            "pr_id": self.pr_id, "author_id": self.author_id,
            "head_repo_id": self.head_repo_id, "head_ref": self.head_ref,
            "base_repo_id": self.base_repo_id, "base_ref": self.base_ref,
            "ci_state": self.ci_state,
            "draft": self.draft,
        }


Fetcher = Callable[[str], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_PENDING = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})
_PASSED = frozenset({"success", "neutral", "skipped"})


def _classify_ci(head_sha: str, checks: list[dict[str, Any]],
                 commit_status: dict[str, Any] | None, *, incomplete: bool) -> str:
    """Only complete, current-head evidence can be a repairable failure."""
    if incomplete or not head_sha:
        return "incomplete"
    failed = False
    pending = False
    for item in checks:
        if not isinstance(item, dict):
            return "incomplete"
        item_sha = item.get("head_sha")
        if item_sha != head_sha:
            return "incomplete"
        run_status = str(item.get("status") or "").casefold()
        conclusion = str(item.get("conclusion") or "").casefold()
        if run_status in _PENDING:
            pending = True
        elif run_status == "completed" and conclusion in _FAILED:
            failed = True
        elif run_status != "completed" or conclusion not in _PASSED:
            return "incomplete"

    classic_count = 0
    if commit_status is not None:
        statuses = commit_status.get("statuses")
        total = commit_status.get("total_count")
        if (not isinstance(statuses, list) or not isinstance(total, int)
                or isinstance(total, bool) or total < 0 or total != len(statuses)
                or any(not isinstance(item, dict) for item in statuses)):
            return "incomplete"
        status_sha = commit_status.get("sha")
        if status_sha != head_sha:
            return "incomplete"
        classic_count = total
        classic_state = str(commit_status.get("state") or "").casefold()
        classic_states = {str(item.get("state") or "").casefold() for item in statuses}
        if not classic_states <= {"success", "failure", "error", "pending"}:
            return "incomplete"
        expected_state = (
            "failure" if classic_states & {"failure", "error"} else
            "pending" if not classic_count or "pending" in classic_states else "success"
        )
        if classic_state != expected_state:
            return "incomplete"
        if classic_count:
            if "pending" in classic_states:
                pending = True
            elif classic_state in {"failure", "error"}:
                failed = True
            elif classic_state != "success":
                return "incomplete"
        elif classic_state != "pending":
            return "incomplete"
    if pending:
        return "pending"
    if failed:
        return "failed"
    if not checks and not classic_count:
        return "unknown"
    return "passed"


def classify_pr(pr: dict[str, Any], checks: list[dict[str, Any]], reviews: list[dict[str, Any]],
                comments: list[dict[str, Any]] | None = None, *, incomplete: bool = False,
                checks_total: int | None = None,
                commit_status: dict[str, Any] | None = None,
                issue_comments: list[dict[str, Any]] | None = None,
                secret_values: Iterable[str] = ()) -> PRObservation:
    """Classify one PR; retain only bounded, redacted, untrusted review evidence."""
    fragments = _secret_fragments(secret_values)
    state = str(pr.get("state", "unknown")).casefold()
    if state not in {"open", "closed"}:
        state = "unknown"
    draft = bool(pr.get("draft"))
    incomplete = (incomplete or (checks_total is not None and checks_total > len(checks))
                  or len(reviews) >= 100 or len(comments or []) >= 100
                  or len(issue_comments or []) >= 100)
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head_sha = _safe_sha(head.get("sha"))
    if _contains_secret(head_sha, fragments):
        head_sha = ""
    # The GET already requests GitHub's latest filter. A name/suite pair does
    # not prove an older failing run was superseded, so retain every returned
    # row rather than hiding possible failure evidence locally.
    effective_checks = checks
    ci_state = _classify_ci(head_sha, effective_checks, commit_status, incomplete=incomplete)
    failed = sum(1 for item in effective_checks if isinstance(item, dict)
                 and str(item.get("conclusion", "")).casefold() in _FAILED)
    pending = sum(1 for item in effective_checks if isinstance(item, dict)
                  and str(item.get("status", "")).casefold() in _PENDING)
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
    elif ci_state == "incomplete":
        status = "incomplete_evidence"
    elif ci_state == "failed":
        status = "checks_failed"
    elif ci_state == "pending":
        status = "checks_pending"
    elif ci_state == "unknown" and "approved" in review_states:
        status = "incomplete_evidence"
    elif changes_requested:
        status = "changes_requested"
    elif "approved" in review_states:
        status = "ready_for_human_merge"
    else:
        status = "waiting_review"
    review_state = "changes_requested" if changes_requested else (
        "approved" if "approved" in review_states else "waiting"
    )
    if incomplete:
        review_state = "incomplete"
    safe_checks = tuple({
        "name": _safe_text(item.get("name"), limit=120, secret_fragments=fragments),
        "status": _safe_text(item.get("status"), limit=32, secret_fragments=fragments),
        "conclusion": _safe_text(item.get("conclusion"), limit=32, secret_fragments=fragments),
    } for item in effective_checks[:30] if isinstance(item, dict))
    notes: list[dict[str, Any]] = []
    for kind, rows in (("review", reviews), ("inline", comments or []),
                       ("issue", issue_comments or [])):
        for item in rows:
            if not isinstance(item, dict) or not item.get("body"):
                continue
            note: dict[str, Any] = {
                "kind": kind, "id": _safe_id(item.get("id")),
                "body": _untrusted_text(item.get("body"), secret_fragments=fragments),
            }
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            if user.get("id") is not None:
                note["author_id"] = _safe_id(user.get("id"))
            if kind == "inline":
                note["path"] = _safe_text(item.get("path"), limit=300,
                                          secret_fragments=fragments)
                note["commit_id"] = _safe_text(item.get("commit_id"), limit=64,
                                               secret_fragments=fragments)
            created = item.get("submitted_at") if kind == "review" else item.get("created_at")
            if created:
                note["created_at"] = _safe_text(created, limit=40, secret_fragments=fragments)
            notes.append(note)
    # Reserve space for each source so long review history cannot hide a newer
    # issue or inline comment. Preserve source order for existing consumers.
    by_kind = {kind: [note for note in notes if note["kind"] == kind]
               for kind in ("review", "inline", "issue")}
    selected: list[dict[str, Any]] = []
    for kind, quota in (("review", 4), ("inline", 3), ("issue", 3)):
        selected.extend(by_kind[kind][-quota:])
    for kind, quota in (("review", 4), ("inline", 3), ("issue", 3)):
        for note in reversed(by_kind[kind][:-quota]):
            if len(selected) >= 10:
                break
            selected.append(note)
    notes = selected
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    author = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    return PRObservation(
        number=_safe_id(pr.get("number")), url=_safe_text(
            pr.get("html_url"), limit=300, secret_fragments=fragments,
        ),
        state=state, head_sha=head_sha, status=status,
        checks_total=max(len(checks), checks_total or 0), checks_failed=failed, checks_pending=pending,
        review_state=review_state, changes_requested=changes_requested,
        checks=safe_checks, review_notes=tuple(notes),
        pr_body=_untrusted_text(pr.get("body"), secret_fragments=fragments)
        if pr.get("body") else None,
        pr_id=_safe_id(pr.get("id")), author_id=_safe_id(author.get("id")),
        head_repo_id=_safe_id(head_repo.get("id")), head_ref=_safe_text(
            head.get("ref"), limit=255, secret_fragments=fragments,
        ),
        base_repo_id=_safe_id(base_repo.get("id")), base_ref=_safe_text(
            base.get("ref"), limit=255, secret_fragments=fragments,
        ),
        ci_state=ci_state, draft=draft,
    )


class GitHubPRObserver:
    """Fetch and classify GitHub PR state through GET-only requests."""

    def __init__(self, token: str, owner: str, repo: str, *, fetcher: Fetcher | None = None,
                 clock: Callable[[], float] = time.time,
                 secret_values: Iterable[str] = ()) -> None:
        self._token, self._owner, self._repo = token, owner, repo
        self._fetcher = fetcher
        self._clock = clock
        self._secret_values = tuple(value for value in (*secret_values, token)
                                    if isinstance(value, str) and value)

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
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        sha = _safe_sha(head.get("sha"))
        if (not sha or _contains_secret(sha, _secret_fragments(self._secret_values))
                or _safe_id(pr.get("number")) != int(number)):
            return classify_pr(pr, [], [], incomplete=True,
                               secret_values=self._secret_values)
        checks = await self._get(
            f"/repos/{self._owner}/{self._repo}/commits/{sha}/check-runs?per_page=100&filter=latest"
        )
        commit_status = await self._get(f"/repos/{self._owner}/{self._repo}/commits/{sha}/status")
        reviews = await self._get(f"{base}/reviews?per_page=100")
        comments = await self._get(f"{base}/comments?per_page=100")
        issue_comments = await self._get(
            f"/repos/{self._owner}/{self._repo}/issues/{int(number)}/comments?per_page=100"
        )
        raw_check_rows = checks.get("check_runs") if isinstance(checks, dict) else None
        check_rows = [item for item in raw_check_rows if isinstance(item, dict)] if (
            isinstance(raw_check_rows, list)
        ) else []
        review_rows = [item for item in reviews if isinstance(item, dict)] if (
            isinstance(reviews, list)
        ) else []
        comment_rows = [item for item in comments if isinstance(item, dict)] if (
            isinstance(comments, list)
        ) else []
        issue_rows = [item for item in issue_comments if isinstance(item, dict)] if (
            isinstance(issue_comments, list)
        ) else []
        has_total = (isinstance(checks, dict)
                     and isinstance(checks.get("total_count"), int)
                     and not isinstance(checks.get("total_count"), bool)
                     and checks["total_count"] >= 0)
        total = _safe_id(checks.get("total_count")) if has_total else 0
        valid_rows = (isinstance(raw_check_rows, list)
                      and all(isinstance(item, dict) for item in raw_check_rows)
                      and all(isinstance(rows, list) and all(
                          isinstance(item, dict) for item in rows
                      ) for rows in (reviews, comments, issue_comments)))
        incomplete = (not has_total or not valid_rows or total != len(check_rows)
                      or len(review_rows) >= 100 or len(comment_rows) >= 100
                      or len(issue_rows) >= 100 or not isinstance(commit_status, dict))
        return classify_pr(pr, check_rows, review_rows, comment_rows,
                           incomplete=incomplete, checks_total=total,
                           commit_status=commit_status if isinstance(commit_status, dict) else None,
                           issue_comments=issue_rows, secret_values=self._secret_values)
