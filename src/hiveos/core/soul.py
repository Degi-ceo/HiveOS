"""
soul.py — loads the immutable identity contract, verbatim and in place.

Config/SOUL.md is PROTECTED: never moved, never edited (see SOUL.md itself and
Docs/references/SYNTHESIS.md Part A.4). This module only READS it, from its
canonical repo location, so the new lowercase package can use it without relocating
the file.
"""
from __future__ import annotations

from pathlib import Path

# src/hiveos/core/soul.py -> parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SOUL_PATH = REPO_ROOT / "Config" / "SOUL.md"


def load_soul() -> str:
    """Return the SOUL.md contents verbatim. Raises if missing (must exist)."""
    return SOUL_PATH.read_text(encoding="utf-8")


SOUL = load_soul()
