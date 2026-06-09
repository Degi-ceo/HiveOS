"""
soul.py — loads the immutable identity contract, verbatim and in place.

Config/SOUL.md is PROTECTED: never moved, never edited (see SOUL.md itself and
docs/references/SYNTHESIS.md Part A.4). This module only READS it, from its
canonical repo location, so the new lowercase package can use it without relocating
the file.
"""
from __future__ import annotations

from pathlib import Path

# src/hive/core/soul.py -> parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SOUL_PATH = REPO_ROOT / "Config" / "SOUL.md"


def load_soul() -> str:
    """Return the SOUL.md contents verbatim. Raises if missing (must exist)."""
    return SOUL_PATH.read_text(encoding="utf-8")


_SOUL: str | None = None


def __getattr__(name: str) -> str:
    """Lazy `soul.SOUL` (PEP 562): importing this module for REPO_ROOT/SOUL_PATH does
    NOT read the file; SOUL is read + cached on first access. Keeps `import
    hive.core.config` side-effect-free and tolerant of a transiently-absent SOUL.md."""
    if name == "SOUL":
        global _SOUL
        if _SOUL is None:
            _SOUL = load_soul()
        return _SOUL
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
