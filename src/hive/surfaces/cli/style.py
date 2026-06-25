"""HiveOS CLI dark theme — ANSI tokens matching dashboard/src/styles/theme.css.

The CSS theme and this Python module share the exact same palette so the
CLI REPL and the Jarvis Front dashboard (P-I) look like one product.

Reference: docs/sprints/SPRINT_6_AUTONOMY_LIB.md § P-J1 (CLI style+theme).
This is the foundation module that PR-J1 lands; subsequent J1 commits
build panel/table/sparkline renderers on top.

All functions are pure: ANSI escapes are written directly to the returned
string. Nothing prints, nothing mutates state. Renderers wrap tokens so
output stays deterministic (great for snapshot tests on `theme="minimal"`).
"""
from __future__ import annotations

import os
import sys
from typing import Final


# ── ANSI escapes (no deps) ──────────────────────────────────────────────
_RESET: Final[str] = "\x1b[0m"

# Token → ANSI SGR (Select Graphic Rendition) code.
# Mirrors the CSS palette in dashboard/src/styles/theme.css.
_TOKENS: Final[dict[str, str]] = {
    # Neon accents — foreground
    "cyan":    "38;5;46",     # #39ff14
    "amber":   "38;5;208",    # #ff9f0a
    "rose":    "38;5;196",    # #ff3b30
    "violet":  "38;5;177",    # #c77dff
    "blue":    "38;5;81",     # #7fdfff

    # Neutrals — foreground
    "text":    "38;5;195",    # #cfe
    "dim":     "38;5;240",    # #4a6a5a (matches --text-dim)
    "muted":   "38;5;238",    # #2a3f33
    "ink":     "38;5;232",    # #05080a

    # Modifiers (combine with above via `paint("bold cyan", ...)`)
    "bold":    "1",
    "italic":  "3",
    "underline": "4",
    "inverse": "7",

    # Backgrounds
    "bg-hud":  "48;5;232",    # #05080a
    "bg-panel":"48;5;233",    # #08110d
}


def _is_color_enabled() -> bool:
    """Resolve color enablement: NO_COLOR > --no-color > tty."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("HIVE_NO_COLOR"):
        return False
    return sys.stdout.isatty()


_ENABLED: bool = _is_color_enabled()


def set_color_enabled(enabled: bool) -> None:
    """Override the runtime color decision (used by tests + `--no-color` flag)."""
    global _ENABLED
    _ENABLED = enabled


def is_color_enabled() -> bool:
    return _ENABLED


def paint(token: str, text: str) -> str:
    """Wrap `text` in the ANSI escape(s) for `token`. Multiple tokens
    space-separated (e.g. ``paint("bold cyan", "...")``).

    When color is disabled (NO_COLOR, non-tty, or explicit), returns
    `text` unchanged.
    """
    if not _ENABLED or not text:
        return text
    parts = [_RESET]
    for raw in token.split():
        code = _TOKENS.get(raw)
        if code:
            parts.append(f"\x1b[{code}m")
    parts.append(text)
    parts.append(_RESET)
    return "".join(parts)


# ── Convenience wrappers (what the rest of the CLI will call) ──────────
def cyan(t: str) -> str:    return paint("cyan", t)
def amber(t: str) -> str:   return paint("amber", t)
def rose(t: str) -> str:    return paint("rose", t)
def violet(t: str) -> str:  return paint("violet", t)
def blue(t: str) -> str:    return paint("blue", t)
def dim(t: str) -> str:     return paint("dim", t)
def muted(t: str) -> str:   return paint("muted", t)
def bold(t: str) -> str:    return paint("bold", t)
def bold_cyan(t: str) -> str:  return paint("bold cyan", t)
def bold_amber(t: str) -> str: return paint("bold amber", t)


# ── Sparkline ──────────────────────────────────────────────────────────
_BAR_CHARS: Final[str] = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], *, width: int = 20) -> str:
    """Render `values` as a unicode-block sparkline. Empty list → empty string.
    Single value → one char at midpoint. Constant series → all mid bars.
    """
    if not values or width <= 0:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _BAR_CHARS[3] * min(len(values), width)
    span = hi - lo
    # Downsample to `width` if longer
    if len(values) > width:
        bucket = len(values) / width
        values = [sum(values[int(i * bucket):int((i + 1) * bucket)]) /
                  max(1, int((i + 1) * bucket) - int(i * bucket))
                  for i in range(width)]
    chars = []
    for v in values:
        idx = int((v - lo) / span * (len(_BAR_CHARS) - 1))
        chars.append(_BAR_CHARS[max(0, min(len(_BAR_CHARS) - 1, idx))])
    return "".join(chars)


# ── Status pill (for surface indicators) ───────────────────────────────
def status_pill(label: str, status: str) -> str:
    """Render a channel-status pill like ``[telegram ✓]`` or ``[slack ✗]``.

    `status` is one of: ok / warn / error / off.
    """
    marks = {"ok": "✓", "warn": "!", "error": "✗", "off": "·"}
    colors = {"ok": "cyan", "warn": "amber", "error": "rose", "off": "dim"}
    mark = marks.get(status, "?")
    color = colors.get(status, "dim")
    return paint(color, f"[{label} {mark}]")


# ── Banner (single-source ASCII HIVE mark) ─────────────────────────────
_BANNER_ASCII: Final[str] = r"""
   ▄█    █▄   ▄█  ███▄▄▄▄    ▄██████▄
   ███    ███ ███  ███▀▀▀██▄ ███    ███
   ███    ███ ███▌ ███   ███ ███    ███
   ███    ███ ███▌ ███   ███ ███    ███
   ███    ███ ███▌ ███   ███ ███    ███
   ███    ███ ███  ███   ███ ███    ███
   ███    ███ ███  ███▄▄▄██▀ ███    ███
   ███    ███ ███  █████▀▀  ▀██████▀
   ███   ███  ███▌ ███
   ████████▀  █▀   ███
                      ▀
"""


def banner(version: str | None = None) -> str:
    """Return the multi-line ASCII HIVE banner. Color: cyan with amber accent.

    Pure: caller decides when to print. `version` is rendered as a sub-line.
    """
    out = paint("cyan", _BANNER_ASCII)
    if version:
        out += paint("amber", f"   v{version}\n")
    return out