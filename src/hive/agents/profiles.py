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
    allowed_child_roles: frozenset[str] = frozenset()
    max_delegation_depth: int = 0
    max_children: int = 0
    max_worker_turns: int = 2
    max_worker_tool_calls: int = 2
    max_worker_seconds: int = 30
    max_branch_turns: int = 0
    max_branch_tool_calls: int = 0
    max_branch_seconds: int = 0
    max_active_children: int = 0


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
        "coder", True, 2, _READ_ONLY_TOOLS | {"propose_candidate_file"},
        requires_independent_review=True,
    ),
    # A coordinator is a full, locally supervised agent.  Its authority is
    # deliberately limited to read-only investigation plus creation of a
    # small, closed set of leaf specialists.  It is never allowed to create
    # another coordinator, so delegation cannot recurse without bound.
    "coordinator": SpecialistProfile(
        "coordinator", True, 1, _READ_ONLY_TOOLS | {"delegate_to_specialist"},
        allowed_child_roles=frozenset({
            "researcher", "coder", "reviewer", "memory-keeper", "security-reviewer",
        }),
        max_delegation_depth=1,
        max_children=3,
        # The root reserves its own bounded execution allowance at creation;
        # each child receives at most this smaller allowance from the single
        # durable branch budget.  These are conservative reservations, not
        # replenished by partial use, so a branch cannot overspend by racing.
        max_worker_turns=4,
        max_worker_tool_calls=4,
        max_worker_seconds=20,
        max_branch_turns=10,
        max_branch_tool_calls=10,
        max_branch_seconds=110,
        max_active_children=1,
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
