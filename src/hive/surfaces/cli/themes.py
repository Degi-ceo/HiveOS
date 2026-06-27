"""Theme registry for the HiveOS CLI — neon / minimal / mono."""
from __future__ import annotations

import os
import sys

from . import style


class Theme:
    """Flat token bag: token name → ANSI SGR code."""
    __slots__ = ("name", "tokens")

    def __init__(self, name: str, tokens: dict[str, str]) -> None:
        self.name = name
        self.tokens = tokens

    def __repr__(self) -> str:
        return f"Theme(name={self.name!r}, tokens=<{len(self.tokens)} keys>)"

    def __getitem__(self, key: str) -> str:
        return self.tokens[key]


NEON: Theme = Theme("neon", dict(style._TOKENS))


MINIMAL: Theme = Theme("minimal", {
    "cyan": "36", "amber": "33", "rose": "31", "violet": "35", "blue": "34",
    "text": "37", "dim": "90", "muted": "37", "ink": "30",
    "bold": "1", "italic": "3", "underline": "4", "inverse": "7",
    "bg-hud": "40", "bg-panel": "47",
})


MONO: Theme = Theme("mono", {
    "bold": "1", "italic": "3", "underline": "4", "inverse": "7",
    "dim": "2", "text": "0", "ink": "0", "muted": "0",
    "cyan": "0", "amber": "0", "rose": "0", "violet": "0", "blue": "0",
    "bg-hud": "40", "bg-panel": "40",
})


REGISTRY: dict[str, Theme] = {"neon": NEON, "minimal": MINIMAL, "mono": MONO}


def _resolve() -> Theme:
    name = os.environ.get("HIVE_THEME", "").lower()
    if name and name in REGISTRY:
        return REGISTRY[name]
    if os.environ.get("NO_COLOR") or os.environ.get("HIVE_NO_COLOR"):
        return MINIMAL
    if sys.stdout.isatty():
        return NEON
    return MINIMAL


_ACTIVE: Theme = _resolve()


def current() -> Theme:
    """Return the active theme (resolved at import; updated by set_theme)."""
    return _ACTIVE


def set_theme(name: str) -> None:
    """Swap the active theme + propagate token changes to style._TOKENS."""
    if name not in REGISTRY:
        raise ValueError(
            f"unknown theme: {name!r}; known: {sorted(REGISTRY)}"
        )
    global _ACTIVE
    _ACTIVE = REGISTRY[name]
    style._TOKENS.clear()
    style._TOKENS.update(_ACTIVE.tokens)
