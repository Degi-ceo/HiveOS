"""
discovery.py — discovery-first engine.

HARD RULE: before building anything, Hive searches official/reputable sources
for an existing skill / MCP server / tool / library, then AUDITS it for safety
before adopting. Results are remembered so the same search is never repeated.

Official sources (in priority order):
  1. Anthropic Agent Skills      — anthropics/skills, agentskills.io
  2. Official MCP Registry        — registry.modelcontextprotocol.io
  3. Reference MCP servers        — modelcontextprotocol/servers
  4. Reputable marketplaces       — smithery, mcp.so, glama, pulsemcp
  5. GitHub repo search           — via GitHub API (deep audit before reuse)

This module returns candidates + a safety verdict. Adoption (copying into the
system) is a separate, gated step.
"""
from __future__ import annotations
import logging
import httpx

from memory.brain import brain

log = logging.getLogger("hiveos.discovery")

SKILL_SOURCES = {
    "anthropic_skills": "https://api.github.com/repos/anthropics/skills/contents",
    "mcp_registry": "https://registry.modelcontextprotocol.io/v0/servers",
    "mcp_servers": "https://api.github.com/repos/modelcontextprotocol/servers/contents/src",
}

# Heuristic red flags for the safety audit (deep audit augments these).
RED_FLAGS = [
    "eval(", "exec(", "os.system", "subprocess.Popen(\"curl",
    "base64.b64decode", "/etc/passwd", "rm -rf", "0.0.0.0",
    "requests.post(\"http", "exfiltrat", "reverse shell",
]


async def discover(need: str, github_token: str = "") -> dict:
    """Search sources for an existing solution to `need`. Remembers the result."""
    # 1. Have we already discovered this? (no repeated research)
    prior = brain.recall(f"discovery {need}", limit=1)
    if prior:
        return {"need": need, "cached": True, "result": prior[0]}

    candidates: list[dict] = []
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        # MCP registry search
        try:
            r = await c.get(SKILL_SOURCES["mcp_registry"], params={"search": need})
            for s in r.json().get("servers", [])[:5]:
                candidates.append({
                    "source": "mcp_registry",
                    "name": s.get("name"),
                    "url": s.get("repository", {}).get("url", ""),
                })
        except Exception as e:  # noqa: BLE001
            log.debug("mcp registry search failed: %s", e)
        # GitHub code/repo search
        try:
            r = await c.get("https://api.github.com/search/repositories",
                            params={"q": f"{need} mcp OR skill", "sort": "stars",
                                    "per_page": 5})
            for repo in r.json().get("items", [])[:5]:
                candidates.append({
                    "source": "github",
                    "name": repo.get("full_name"),
                    "url": repo.get("html_url"),
                    "stars": repo.get("stargazers_count", 0),
                })
        except Exception as e:  # noqa: BLE001
            log.debug("github search failed: %s", e)

    result = {"need": need, "cached": False, "candidates": candidates}
    brain.learn("research", f"discovery {need}",
                f"Discovered candidates: {candidates}", "discovery-engine")
    return result


async def audit_repo(raw_url: str, github_token: str = "") -> dict:
    """Lightweight safety audit of a candidate file/repo. Deep audit can be
    delegated to mcpserver-audit (CSA project) once adopted as a tool."""
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    flags: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            text = (await c.get(raw_url)).text.lower()
            flags = [f for f in RED_FLAGS if f.lower() in text]
    except Exception as e:  # noqa: BLE001
        return {"safe": None, "error": str(e)}
    return {"safe": len(flags) == 0, "flags": flags,
            "verdict": "review-needed" if flags else "looks-clean"}
