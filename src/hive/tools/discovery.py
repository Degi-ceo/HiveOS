"""Discovery-first engine with explicit, non-operative provenance records."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Protocol

import httpx

log = logging.getLogger("hive.tools.discovery")
SKILL_SOURCES = {"mcp_registry": "https://registry.modelcontextprotocol.io/v0/servers", "github_repos": "https://api.github.com/search/repositories"}
RED_FLAGS = ("eval(", "exec(", "os.system", "subprocess.popen(\"curl", "base64.b64decode", "/etc/passwd", "rm -rf", "exfiltrat", "reverse shell")


class MemoryLike(Protocol):
    def recall(self, query: str, limit: int = 5) -> list[dict[str, str]]: ...
    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None: ...


class DiscoveryRecorder(Protocol):
    def record(self, **kwargs: Any) -> Any: ...


def _decision_key(need: str, outcome: str, candidate: dict[str, Any] | None = None) -> str:
    payload = json.dumps({"need": need.strip().lower(), "outcome": outcome, "candidate": candidate or {}}, sort_keys=True, default=str)
    return f"discovery:{hashlib.sha256(payload.encode()).hexdigest()}"


def _record(recorder: DiscoveryRecorder | None, *, need: str, outcome: str, candidate: dict[str, Any] | None = None, rationale: str) -> None:
    if recorder is None:
        return
    candidate = candidate or {}
    try:
        recorder.record(capability_key=need.strip().lower()[:200], phase="discovery", outcome=outcome,
            idempotency_key=_decision_key(need, outcome, candidate), candidate_name=str(candidate.get("name", "")),
            candidate_source=str(candidate.get("source", "")), candidate_url=str(candidate.get("url", "")),
            audit_status="not_run", rationale=rationale, recorded_by="discovery-engine")
    except Exception as exc:  # noqa: BLE001 - audit recording must not block read-only discovery
        log.warning("discovery decision recording failed: %s", type(exc).__name__)


def scan_red_flags(text: str) -> list[str]:
    """Pure heuristic scan; returns the matched red flags (case-insensitive)."""
    low = text.lower()
    return [flag for flag in RED_FLAGS if flag in low]


async def discover(need: str, *, memory: MemoryLike | None = None, github_token: str = "", limit: int = 5, security_delegate: Callable | None = None, recorder: DiscoveryRecorder | None = None) -> dict:
    """Search sources for an existing solution; cache and audit-record the result."""
    if memory is not None:
        prior = memory.recall(f"discovery {need}", 1)
        if prior:
            _record(recorder, need=need, outcome="reused", rationale="reused memory-cached discovery result")
            return {"need": need, "cached": True, "result": prior[0]}

    candidates: list[dict] = []
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        try:
            response = await client.get(SKILL_SOURCES["mcp_registry"], params={"search": need})
            for server in response.json().get("servers", [])[:limit]:
                candidates.append({"source": "mcp_registry", "name": server.get("name"), "url": server.get("repository", {}).get("url", "")})
        except Exception as exc:  # noqa: BLE001
            log.debug("mcp registry search failed: %s", exc)
        try:
            response = await client.get(SKILL_SOURCES["github_repos"], params={"q": f"{need} mcp OR skill", "sort": "stars", "per_page": limit})
            for repo in response.json().get("items", [])[:limit]:
                candidates.append({"source": "github", "name": repo.get("full_name"), "url": repo.get("html_url"), "stars": repo.get("stargazers_count", 0)})
        except Exception as exc:  # noqa: BLE001
            log.debug("github search failed: %s", exc)
    if security_delegate is not None:
        for candidate in candidates:
            if candidate.get("url"):
                try:
                    candidate["security_note"] = await security_delegate(f"Audit {candidate.get('name', '?')} at {candidate['url']}")
                except Exception:  # noqa: BLE001
                    candidate["security_note"] = "[audit unavailable]"
    _record(recorder, need=need, outcome="found" if candidates else "no_match", candidate=candidates[0] if candidates else None, rationale="web discovery completed; no candidate was installed or enabled")
    if memory is not None:
        memory.learn("research", f"discovery {need}", f"candidates: {candidates}", "discovery-engine")
    return {"need": need, "cached": False, "candidates": candidates}


async def audit_repo(raw_url: str, *, github_token: str = "") -> dict:
    """Lightweight safety audit of a candidate file. Deep audit is a gated follow-up."""
    headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            flags = scan_red_flags((await client.get(raw_url)).text)
    except Exception as exc:  # noqa: BLE001
        return {"safe": None, "error": str(exc)}
    return {"safe": not flags, "flags": flags, "verdict": "review-needed" if flags else "looks-clean"}
