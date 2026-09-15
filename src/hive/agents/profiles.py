"""Enforceable least-privilege profiles for Hive specialist workers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecialistProfile:
    name: str
    read_only: bool
    max_attempts: int
    requires_independent_review: bool = False


_PROFILES = {
    "researcher": SpecialistProfile("researcher", True, 2),
    "reviewer": SpecialistProfile("reviewer", True, 2),
    "security-reviewer": SpecialistProfile("security-reviewer", True, 2),
    "memory-keeper": SpecialistProfile("memory-keeper", False, 2),
    "coder": SpecialistProfile("coder", False, 2, requires_independent_review=True),
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
