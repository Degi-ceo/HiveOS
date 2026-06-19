"""
discovery.py — discovery-first engine (KEEP from Tools/discovery.py, HiveOS DNA).

HARD RULE (CLAUDE.md): before building anything, search official/reputable sources
for an existing skill / MCP server / library, AUDIT it, and remember the result so
the search is never repeated.

Layer discipline: the old module imported memory.brain directly. To keep
tools -> core only, memory is injected as an optional `MemoryLike` (recall/learn);
when wired, results are cached and reused; when absent, discovery still works
without caching. Adoption (copying code in) stays a separate, gated step.
"""
from __future__ import annotations

import logging
from typing import Callable, Protocol

import httpx

log = logging.getLogger("hive.tools.discovery")

SKILL_SOURCES = {
    "mcp_registry": "https://registry.modelcontextprotocol.io/v0/servers",
    "github_repos": "https://api.github.com/search/repositories",
}

# Heuristic red flags for the safety audit (deep audit augments these).
RED_FLAGS = (
    "eval(", "exec(", "os.system", "subprocess.popen(\"curl", "base64.b64decode",
    "/etc/passwd", "rm -rf", "exfiltrat", "reverse shell",
)


class MemoryLike(Protocol):
    def recall(self, query: str, limit: int = 5) -> list[dict[str, str]]: ...
    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None: ...


def scan_red_flags(text: str) -> list[str]:
    """Pure heuristic scan; returns the matched red flags (case-insensitive)."""
    low = text.lower()
    return [f for f in RED_FLAGS if f in low]


async def discover(need: str, *, memory: MemoryLike | None = None,
                   github_token: str = "", limit: int = 5,
                   security_delegate: Callable | None = None) -> dict:
    """Search sources for an existing solution to `need`. Caches via memory if given.

    security_delegate: optional async callable(task: str) -> str. When provided, each
    candidate with a URL is audited by calling security_delegate(f"Audit {name} at {url}").
    The result is stored in candidate["security_note"]. Inject from the caller to avoid
    importing hive.agents at module level (DAG constraint)."""
    if memory is not None:
        prior = memory.recall(f"discovery {need}", 1)
        if prior:
            return {"need": need, "cached": True, "result": prior[0]}

    candidates: list[dict] = []
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        try:
            r = await c.get(SKILL_SOURCES["mcp_registry"], params={"search": need})
            for s in r.json().get("servers", [])[:limit]:
                candidates.append({"source": "mcp_registry", "name": s.get("name"),
                                   "url": s.get("repository", {}).get("url", "")})
        except Exception as exc:  # noqa: BLE001 - source best-effort
            log.debug("mcp registry search failed: %s", exc)
        try:
            r = await c.get(SKILL_SOURCES["github_repos"],
                            params={"q": f"{need} mcp OR skill", "sort": "stars",
                                    "per_page": limit})
            for repo in r.json().get("items", [])[:limit]:
                candidates.append({"source": "github", "name": repo.get("full_name"),
                                   "url": repo.get("html_url"),
                                   "stars": repo.get("stargazers_count", 0)})
        except Exception as exc:  # noqa: BLE001
            log.debug("github search failed: %s", exc)

    if security_delegate is not None:
        for c in candidates:
            if c.get("url"):
                try:
                    c["security_note"] = await security_delegate(
                        f"Audit {c.get('name', '?')} at {c['url']}")
                except Exception:  # noqa: BLE001
                    c["security_note"] = "[audit unavailable]"

    if memory is not None:
        memory.learn("research", f"discovery {need}",
                     f"candidates: {candidates}", "discovery-engine")
    return {"need": need, "cached": False, "candidates": candidates}


async def audit_repo(raw_url: str, *, github_token: str = "") -> dict:
    """Lightweight safety audit of a candidate file. Deep audit is a gated follow-up."""
    headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            flags = scan_red_flags((await c.get(raw_url)).text)
    except Exception as exc:  # noqa: BLE001
        return {"safe": None, "error": str(exc)}
    return {"safe": not flags, "flags": flags,
            "verdict": "review-needed" if flags else "looks-clean"}
