"""Tests for hive.surfaces.cli.style — 100% coverage on ANSI token renderer.

Covers paint, _is_color_enabled, set/is_color_enabled, every convenience
wrapper, sparkline (incl. downsampling), status_pill, and banner.

We force color on via `set_color_enabled(True)` so output is deterministic
for snapshot-style assertions.
"""
from __future__ import annotations

import sys

import pytest

from hive.surfaces.cli import style
from hive.surfaces.cli.style import (
    _RESET,
    amber,
    banner,
    blue,
    bold,
    bold_amber,
    bold_cyan,
    cyan,
    dim,
    is_color_enabled,
    muted,
    paint,
    rose,
    set_color_enabled,
    sparkline,
    status_pill,
    violet,
)


@pytest.fixture(autouse=True)
def _force_color():
    """Force color on for deterministic assertions; restore after test."""
    saved = style._ENABLED
    set_color_enabled(True)
    yield
    set_color_enabled(saved)


# ── _is_color_enabled ─────────────────────────────────────────────────
def test_is_color_enabled_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert style._is_color_enabled() is False


def test_is_color_enabled_hive_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("HIVE_NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert style._is_color_enabled() is False


def test_is_color_enabled_tty_true(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert style._is_color_enabled() is True


def test_is_color_enabled_tty_false(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HIVE_NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert style._is_color_enabled() is False


# ── set_color_enabled / is_color_enabled ──────────────────────────────
def test_color_enable_toggle():
    set_color_enabled(True)
    assert is_color_enabled() is True
    set_color_enabled(False)
    assert is_color_enabled() is False


# ── paint ─────────────────────────────────────────────────────────────
def test_paint_single_token_wraps_with_reset():
    out = paint("cyan", "hello")
    assert out.startswith(_RESET)
    assert "\x1b[38;5;46m" in out  # cyan
    assert "hello" in out
    assert out.endswith(_RESET)


def test_paint_multi_token():
    out = paint("bold cyan", "x")
    assert "\x1b[1m" in out   # bold
    assert "\x1b[38;5;46m" in out  # cyan


def test_paint_unknown_token_only_reset():
    out = paint("nope", "x")
    assert out == f"{_RESET}x{_RESET}"


def test_paint_color_disabled_returns_plain():
    set_color_enabled(False)
    assert paint("cyan", "hello") == "hello"


def test_paint_empty_text():
    set_color_enabled(True)
    assert paint("cyan", "") == ""
    set_color_enabled(False)
    assert paint("cyan", "") == ""


# ── Convenience wrappers ─────────────────────────────────────────────
@pytest.mark.parametrize("fn,token", [
    (cyan,   "cyan"),
    (amber,  "amber"),
    (rose,   "rose"),
    (violet, "violet"),
    (blue,   "blue"),
    (dim,    "dim"),
    (muted,  "muted"),
    (bold,   "bold"),
    (bold_cyan,  "bold cyan"),
    (bold_amber, "bold amber"),
])
def test_convenience_wrappers_match_paint(fn, token):
    assert fn("x") == paint(token, "x")


# ── sparkline ─────────────────────────────────────────────────────────
def test_sparkline_empty():
    assert sparkline([]) == ""


def test_sparkline_single_value_uses_midpoint():
    # Constant range → _BAR_CHARS[3] (the midpoint of 8 block chars)
    assert sparkline([42.0]) == style._BAR_CHARS[3]


def test_sparkline_constant_series_all_mid():
    assert sparkline([5.0, 5.0, 5.0, 5.0]) == style._BAR_CHARS[3] * 4


def test_sparkline_ascending():
    out = sparkline([0.0, 1.0, 2.0, 3.0, 4.0])
    assert len(out) == 5
    # First char should be lowest, last should be highest
    assert out[0] == "▁"
    assert out[-1] == "█"


def test_sparkline_width_zero():
    assert sparkline([1.0, 2.0], width=0) == ""


def test_sparkline_width_negative():
    assert sparkline([1.0, 2.0], width=-1) == ""


def test_sparkline_negative_values_rescaled():
    out = sparkline([-2.0, -1.0, 0.0, 1.0, 2.0])
    assert len(out) == 5
    assert out[0] == "▁"
    assert out[-1] == "█"


def test_sparkline_downsamples_to_width():
    # 100 values, width=10 → result is exactly 10 chars
    out = sparkline([float(i) for i in range(100)], width=10)
    assert len(out) == 10


# ── status_pill ───────────────────────────────────────────────────────
@pytest.mark.parametrize("status,mark,color", [
    ("ok",    "✓", "cyan"),
    ("warn",  "!", "amber"),
    ("error", "✗", "rose"),
    ("off",   "·", "dim"),
])
def test_status_pill_known(status, mark, color):
    out = status_pill("telegram", status)
    assert "telegram" in out
    assert mark in out
    assert f"\x1b[38;5;" in out  # some color code present
    # confirm correct color SGR by checking token used
    expected = paint(color, f"[telegram {mark}]")
    assert out == expected


def test_status_pill_unknown_status():
    out = status_pill("x", "bogus")
    assert "?" in out
    assert out == paint("dim", "[x ?]")


# ── banner ────────────────────────────────────────────────────────────
def test_banner_no_version():
    out = banner()
    assert "HIVE" in out or "█" in out  # ascii glyphs present
    assert "v" not in out.split("\n")[-1]  # last non-empty line has no 'v'


def test_banner_with_version():
    out = banner("1.2.3")
    assert "1.2.3" in out


def test_banner_colorizes_version_line():
    out = banner("9.9")
    # amber token SGR is 38;5;208
    assert "\x1b[38;5;208m" in out
