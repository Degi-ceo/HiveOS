"""Enforceable least-privilege profiles for Hive specialist workers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SpecialistProfile:
    name: str
    read_only: bool
    max_attempts: int
    allowed_tools: frozenset[str]
    requires_independent_review: bool = False


_READ_ONLY_TOOLS = frozenset({
    "discover", "github_get_pr", "github_list_commits", "github_list_prs",
    "hive_status", "obsidian_list", "obsidian_read", "obsidian_search",
    "query_memory", "read_file", "web_get",
})

_PROFILES = {
    "researcher": SpecialistProfile("researcher", True, 2, _READ_ONLY_TOOLS),
    "reviewer": SpecialistProfile("reviewer", True, 2, _READ_ONLY_TOOLS),
    "security-reviewer": SpecialistProfile("security-reviewer", True, 2, _READ_ONLY_TOOLS),
    "memory-keeper": SpecialistProfile(
        "memory-keeper", False, 2, _READ_ONLY_TOOLS | {"remember_memory"},
    ),
    "coder": SpecialistProfile(
        "coder", True, 2, _READ_ONLY_TOOLS,
        requires_independent_review=True,
    ),
}


def specialist_profile(name: str) -> SpecialistProfile:
    """Return a known profile; unknown roles are never implicitly privileged."""
    normalized = str(name).strip().casefold()
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown specialist role: {normalized or '<empty>'}") from exc


def specialist_profiles() -> tuple[SpecialistProfile, ...]:
    return tuple(_PROFILES[name] for name in sorted(_PROFILES))


def scoped_specialist_tools(role: str, tools: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fail-closed snapshot of the tools granted to one specialist.

    Newly registered or MCP-provided tools are excluded until a profile explicitly
    permits them.  Leaf workers therefore cannot inherit the CEO's full registry.
    """
    profile = specialist_profile(role)
    return {name: tools[name] for name in profile.allowed_tools if name in tools}
