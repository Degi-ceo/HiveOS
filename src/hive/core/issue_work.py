"""Read-only, fail-closed GitHub issue selection for autonomous work pickup.

Issue content is untrusted. This module returns it only to the execution boundary;
the durable work board stores the repository identity and issue number alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hive.core.redact import register_secret_values

GraphQLFetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
_IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_CODE = re.compile(r"HIVE-[0-9]{3,5}\Z", re.I)
_TITLE_CODE = re.compile(r"^\[(HIVE-[0-9]{3,5})\](?:\s|\[|$)", re.I)
_DECLARATION = re.compile(r"^[ \t]*(?:\*\*)?Dependencies[ \t]*:(?:\*\*)?[ \t]*(.+?)[ \t]*$", re.I | re.M)
_REFERENCE = re.compile(r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/)?#?([0-9]+)\Z")
_MAX_BODY = 12000
_MAX_ISSUES = 500

_CANDIDATES = """query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){
  issues(first:100,states:OPEN,labels:["hive-eligible"],orderBy:{field:UPDATED_AT,direction:DESC}){
    nodes{number} pageInfo{hasNextPage}
  }
}}"""

_ISSUE = """query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){
  issue(number:$number){number state title body
    labels(first:100){nodes{name} pageInfo{hasNextPage}}
    blockedBy(first:100){nodes{number state} pageInfo{hasNextPage}}
    closedByPullRequestsReferences(first:100){nodes{number state} pageInfo{hasNextPage}}
  }
}}"""

_ISSUE_INDEX = """query($owner:String!,$repo:String!,$after:String){repository(owner:$owner,name:$repo){
  issues(first:100,after:$after,states:[OPEN,CLOSED]){
    nodes{number title state} pageInfo{hasNextPage endCursor}
  }
}}"""

_OPEN_PRS = """query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){
  pullRequests(first:100,states:OPEN){nodes{number title body
    closingIssuesReferences(first:100){nodes{number} pageInfo{hasNextPage}}
  } pageInfo{hasNextPage}}
}}"""


@dataclass(frozen=True, slots=True)
class IssueSelection:
    eligible: bool
    number: int
    body: str = ""
    title: str = ""
    reason: str = "ineligible"


def _complete(connection: Any) -> list[dict[str, Any]] | None:
    if not isinstance(connection, dict):
        return None
    page = connection.get("pageInfo")
    nodes = connection.get("nodes")
    if (not isinstance(page, dict) or page.get("hasNextPage") is not False
            or not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes)):
        return None
    return nodes


def _dependencies(body: str) -> tuple[set[int], set[str]] | None:
    matches = list(_DECLARATION.finditer(body))
    # Alternative dependency prose is ambiguous (including references on the
    # next line). Require the canonical single-line declaration instead of
    # guessing which later reference is a prerequisite.
    other_declaration = (
        re.search(r"\b(?:depends on|blocked by|prerequisites?)\b", body, re.I)
        or re.search(r"\brequires\b[^\n]*(?:HIVE-\d|#\d|/issues/\d+)", body, re.I)
        or re.search(
            r"^[ \t]*(?:[-*+][ \t]*)?requires[ \t]*:?[ \t]*$",
            body, re.I | re.M,
        )
    )
    if (len(matches) > 1 or other_declaration
            or (not matches and "dependenc" in body.lower())):
        return None
    if not matches:
        return set(), set()
    # Only a single explicit line is supported. Any adjacent continuation may
    # declare more dependencies and cannot be safely treated as unrelated prose.
    following = body[matches[0].end():].splitlines()[1:]
    if following and following[0].strip():
        return None
    declaration = matches[0].group(1).strip().rstrip(".")
    if declaration.lower() in {"none", "n/a", "no"}:
        return set(), set()
    numbers: set[int] = set()
    codes: set[str] = set()
    for fragment in re.split(r"[,;]", declaration):
        item = fragment.strip().strip("`").strip()
        if _CODE.fullmatch(item):
            codes.add(item.upper())
        elif match := _REFERENCE.fullmatch(item):
            number = int(match.group(1))
            if number < 1:
                return None
            numbers.add(number)
        else:
            return None
    return (numbers, codes) if numbers or codes else None


class GitHubIssueWorkReader:
    """Select opt-in work from one fixed repository; network errors never authorize."""

    def __init__(self, token: str, owner: str, repo: str, *,
                 fetcher: GraphQLFetcher | None = None) -> None:
        if (not token or not all(isinstance(value, str) and value not in {".", ".."}
                                  and _IDENTITY.fullmatch(value)
                                  for value in (owner, repo))):
            raise ValueError("issue work reader requires fixed repository credentials")
        register_secret_values([token])
        self._token, self.owner, self.repo = token, owner, repo
        self._fetcher = fetcher

    async def _query(self, query: str, **variables: Any) -> dict[str, Any] | None:
        args = {"owner": self.owner, "repo": self.repo, **variables}
        try:
            if self._fetcher is not None:
                response = await self._fetcher(query, args)
            else:
                import httpx

                async with httpx.AsyncClient(
                    timeout=20, trust_env=False, follow_redirects=False,
                ) as client:
                    result = await client.post(
                        "https://api.github.com/graphql",
                        headers={"Authorization": f"Bearer {self._token}",
                                 "Accept": "application/vnd.github+json"},
                        json={"query": query, "variables": args},
                    )
                result.raise_for_status()
                if result.status_code != 200:
                    return None
                response = result.json()
        except Exception:  # noqa: BLE001 - any upstream failure means no eligibility
            return None
        if not isinstance(response, dict) or response.get("errors"):
            return None
        data = response.get("data")
        repo = data.get("repository") if isinstance(data, dict) else None
        return repo if isinstance(repo, dict) else None

    async def candidate_numbers(self) -> tuple[int, ...] | None:
        repo = await self._query(_CANDIDATES)
        nodes = _complete(repo.get("issues")) if repo else None
        if nodes is None:
            return None
        numbers = [node.get("number") for node in nodes]
        if (any(type(number) is not int or number <= 0 for number in numbers)
                or len(set(numbers)) != len(numbers)):
            return None
        return tuple(numbers)

    async def _issue_index(self) -> dict[str, dict[str, Any]] | None:
        index: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_ISSUES // 100):
            repo = await self._query(_ISSUE_INDEX, after=cursor)
            connection = repo.get("issues") if repo else None
            if not isinstance(connection, dict):
                return None
            page = connection.get("pageInfo")
            nodes = connection.get("nodes")
            if (not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool
                    or not isinstance(nodes, list)
                    or not all(isinstance(node, dict) for node in nodes)):
                return None
            for node in nodes:
                title = node.get("title")
                if (type(node.get("number")) is not int or not isinstance(title, str)
                        or node.get("state") not in {"OPEN", "CLOSED"}):
                    return None
                match = _TITLE_CODE.match(title)
                if match:
                    code = match.group(1).upper()
                    if code in index:
                        return None  # ambiguous code, even if both are closed
                    index[code] = node
            if page["hasNextPage"] is False:
                return index
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return None
            seen.add(cursor)
        return None

    async def inspect(self, number: int) -> IssueSelection:
        invalid = IssueSelection(False, number)
        if type(number) is not int or number <= 0:
            return invalid
        repo = await self._query(_ISSUE, number=number)
        issue = repo.get("issue") if repo else None
        if not isinstance(issue, dict) or issue.get("number") != number:
            return invalid
        labels = _complete(issue.get("labels"))
        blocked = _complete(issue.get("blockedBy"))
        linked_prs = _complete(issue.get("closedByPullRequestsReferences"))
        title, body = issue.get("title"), issue.get("body")
        if (issue.get("state") != "OPEN" or labels is None or blocked is None
                or linked_prs is None or not isinstance(title, str)
                or not isinstance(body, str) or len(title) > 300
                or len(body) > _MAX_BODY):
            return invalid
        if (not all(isinstance(label.get("name"), str) for label in labels)
                or "hive-eligible" not in {label["name"] for label in labels}):
            return invalid
        declared = _dependencies(body)
        if declared is None:
            return invalid
        numbers, codes = declared
        if any(type(node.get("number")) is not int or node.get("state") != "CLOSED"
               for node in blocked):
            return invalid
        if any(type(node.get("number")) is not int or node.get("state") == "OPEN"
               or node.get("state") not in {"OPEN", "CLOSED", "MERGED"}
               for node in linked_prs):
            return invalid
        index = await self._issue_index() if codes else {}
        if index is None:
            return invalid
        for code in codes:
            match = index.get(code)
            if match is None or match.get("state") != "CLOSED":
                return invalid
            numbers.add(match["number"])
        for dependency in numbers:
            if dependency == number:
                return invalid
            dep_repo = await self._query(_ISSUE, number=dependency)
            dep = dep_repo.get("issue") if dep_repo else None
            if (not isinstance(dep, dict) or dep.get("number") != dependency
                    or dep.get("state") != "CLOSED"):
                return invalid
        pr_repo = await self._query(_OPEN_PRS)
        prs = _complete(pr_repo.get("pullRequests")) if pr_repo else None
        if prs is None:
            return invalid
        issue_ref = re.compile(
            rf"(?<![0-9])#{number}(?![0-9])|"
            rf"https://github\.com/{re.escape(self.owner)}/{re.escape(self.repo)}"
            rf"/issues/{number}(?![0-9])",
            re.I,
        )
        issue_code_match = _TITLE_CODE.match(title)
        issue_code_ref = (
            re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(issue_code_match.group(1))}"
                r"(?![A-Za-z0-9-])", re.I,
            ) if issue_code_match else None
        )
        for pr in prs:
            references = _complete(pr.get("closingIssuesReferences"))
            if (references is None or type(pr.get("number")) is not int
                    or not isinstance(pr.get("title"), str)
                    or not isinstance(pr.get("body"), str)
                    or len(pr["title"]) > 300 or len(pr["body"]) > _MAX_BODY):
                return invalid
            if any(ref.get("number") == number for ref in references):
                return invalid
            pr_text = pr["title"] + "\n" + pr["body"]
            if issue_ref.search(pr_text) or (
                issue_code_ref is not None and issue_code_ref.search(pr_text)
            ):
                return invalid
        return IssueSelection(True, number, body, title, "eligible")
