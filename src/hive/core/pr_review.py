"""Fail-closed, read-only selection of small GitHub review suggestions.

GitHub review text is untrusted data.  This module never grants PR write
authority: the caller must separately authenticate the Hive creation receipt,
reserve a durable feedback round, and re-fetch the same signal before a push.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hive.core.redact import redact_known_secrets, register_secret_values

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_SUGGESTION = re.compile(r"```suggestion\r?\n([^\r\n]{1,200})\r?\n```\Z")
_QUERY = """query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      headRefOid
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id isResolved isOutdated path line startLine diffSide subjectType
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              id body state outdated line startLine replyTo { id }
              author { ... on User { databaseId } }
              commit { oid }
            }
          }
        }
      }
    }
  }
}"""

GraphQLFetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ReviewSuggestion:
    """Transient replacement from one authenticated, current-head review thread."""

    thread_id: str
    comment_id: str
    reviewer_id: int
    path: str
    line: int
    replacement: str
    head_sha: str
    signal_digest: str


@dataclass(frozen=True, slots=True)
class ReviewSelection:
    """`ready`, `none`, `ambiguous`, or `invalid`; never an authorization token."""

    status: str
    suggestion: ReviewSuggestion | None = None


def _doc_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 255 or "\\" in value:
        return ""
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != "docs" or any(
        part in {".", ".."} or _COMPONENT.fullmatch(part) is None
        for part in parts[1:]
    ):
        return ""
    return value if value.rsplit("/", 1)[-1].endswith((".md", ".rst", ".txt")) else ""


def _candidate(thread: dict[str, Any], head_sha: str,
               reviewer_ids: frozenset[int]) -> tuple[bool, ReviewSuggestion | None]:
    """Return whether an allowed reviewer owns this thread, plus a safe candidate."""
    comments = thread.get("comments")
    if not isinstance(comments, dict):
        return False, None
    nodes = comments.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > 100:
        return False, None
    root = nodes[0]
    if not isinstance(root, dict):
        return False, None
    author = root.get("author")
    reviewer_id = author.get("databaseId") if isinstance(author, dict) else None
    if type(reviewer_id) is not int or reviewer_id not in reviewer_ids:
        return False, None
    if thread.get("isResolved") is True or thread.get("isOutdated") is True:
        return False, None
    # An incomplete thread is not evidence that the selected root is still
    # current or unchallenged. Never act on a partially paginated thread.
    page = comments.get("pageInfo")
    if (not isinstance(page, dict) or page.get("hasNextPage") is not False
            or len(nodes) != 1):
        return True, None
    body = root.get("body")
    matched = _SUGGESTION.fullmatch(body) if isinstance(body, str) else None
    path = _doc_path(thread.get("path"))
    line = thread.get("line")
    start_line = thread.get("startLine")
    commit = root.get("commit")
    if (
        thread.get("isResolved") is not False
        or thread.get("isOutdated") is not False
        or thread.get("diffSide") != "RIGHT"
        or thread.get("subjectType") != "LINE"
        or not path or type(line) is not int or not 1 <= line <= 100_000
        or start_line not in {None, line}
        or root.get("line") != line
        or root.get("startLine") not in {None, line}
        or root.get("replyTo") is not None
        or root.get("state") != "SUBMITTED"
        or root.get("outdated") is not False
        or not isinstance(commit, dict) or commit.get("oid") != head_sha
        or not matched or redact_known_secrets(body) != body
        or not isinstance(thread.get("id"), str)
        or not isinstance(root.get("id"), str)
        or len(thread["id"]) > 200 or len(root["id"]) > 200
    ):
        return True, None
    replacement = matched.group(1)
    if (replacement.strip() != replacement or "```" in replacement or any(
        unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
        for char in replacement
    )):
        return True, None
    digest = hashlib.sha256(
        f"{thread['id']}\0{root['id']}\0{head_sha}\0{body}".encode("utf-8")
    ).hexdigest()
    return True, ReviewSuggestion(
        thread_id=thread["id"], comment_id=root["id"],
        reviewer_id=reviewer_id, path=path, line=line,
        replacement=replacement, head_sha=head_sha, signal_digest=digest,
    )


def select_review_suggestion(
    response: object, *, number: int, head_sha: str,
    reviewer_ids: frozenset[int],
) -> ReviewSelection:
    """Select exactly one current, one-line docs suggestion or stand down."""
    if (
        type(number) is not int or number <= 0
        or not isinstance(head_sha, str) or _OID.fullmatch(head_sha) is None
        or not reviewer_ids or any(type(item) is not int or item <= 0 for item in reviewer_ids)
        or not isinstance(response, dict) or response.get("errors")
    ):
        return ReviewSelection("invalid")
    data = response.get("data")
    repo = data.get("repository") if isinstance(data, dict) else None
    pr = repo.get("pullRequest") if isinstance(repo, dict) else None
    threads = pr.get("reviewThreads") if isinstance(pr, dict) else None
    if (
        not isinstance(pr, dict) or type(pr.get("number")) is not int
        or pr["number"] != number or pr.get("headRefOid") != head_sha
        or not isinstance(threads, dict)
        or not isinstance(threads.get("pageInfo"), dict)
        or threads["pageInfo"].get("hasNextPage") is not False
        or not isinstance(threads.get("nodes"), list)
        or len(threads["nodes"]) > 100
    ):
        return ReviewSelection("invalid")
    selected: list[ReviewSuggestion] = []
    ambiguous = False
    for thread in threads["nodes"]:
        if not isinstance(thread, dict):
            return ReviewSelection("invalid")
        comments = thread.get("comments")
        if (
            type(thread.get("isResolved")) is not bool
            or type(thread.get("isOutdated")) is not bool
            or not isinstance(comments, dict)
            or not isinstance(comments.get("pageInfo"), dict)
            or comments["pageInfo"].get("hasNextPage") is not False
            or not isinstance(comments.get("nodes"), list)
            or not 1 <= len(comments["nodes"]) <= 100
            or any(not isinstance(comment, dict) for comment in comments["nodes"])
        ):
            return ReviewSelection("invalid")
        belongs_to_reviewer, candidate = _candidate(thread, head_sha, reviewer_ids)
        if belongs_to_reviewer:
            if candidate is None:
                ambiguous = True
            else:
                selected.append(candidate)
    if ambiguous or len(selected) > 1:
        return ReviewSelection("ambiguous")
    return ReviewSelection("ready", selected[0]) if selected else ReviewSelection("none")


class GitHubReviewReader:
    """Read only complete review threads from one fixed GitHub repository."""

    def __init__(self, token: str, owner: str, repo: str, *,
                 fetcher: GraphQLFetcher | None = None) -> None:
        if not token or not all(
            value not in {".", ".."}
            and re.fullmatch(r"[A-Za-z0-9_.-]+", value or "")
            for value in (owner, repo)
        ):
            raise ValueError("GitHub review reader requires fixed repository credentials")
        register_secret_values([token])
        self._token, self._owner, self._repo = token, owner, repo
        self._fetcher = fetcher

    async def select(self, number: int, head_sha: str,
                     reviewer_ids: frozenset[int]) -> ReviewSelection:
        if type(number) is not int or number <= 0:
            return ReviewSelection("invalid")
        variables = {"owner": self._owner, "repo": self._repo, "number": number}
        try:
            if self._fetcher is not None:
                response = await self._fetcher(_QUERY, variables)
            else:
                import httpx

                async with httpx.AsyncClient(timeout=20) as client:
                    result = await client.post(
                        "https://api.github.com/graphql",
                        headers={"Authorization": f"Bearer {self._token}",
                                 "Accept": "application/vnd.github+json"},
                        json={"query": _QUERY, "variables": variables},
                    )
                result.raise_for_status()
                response = result.json()
        except Exception:  # noqa: BLE001 - network failure never authorizes a write
            return ReviewSelection("invalid")
        return select_review_suggestion(
            response, number=number, head_sha=head_sha, reviewer_ids=reviewer_ids,
        )
