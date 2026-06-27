"""Output singleton — wraps style.paint + print for the HiveOS CLI."""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import style

if TYPE_CHECKING:
    from .themes import Theme


class Output:
    """The CLI's rendering handle. Wraps style.paint + print()."""

    def __init__(self, *, theme: "Theme | None" = None, color: bool | None = None) -> None:
        from .themes import current as _current_theme
        self.theme = theme if theme is not None else _current_theme()
        self.color = color if color is not None else style.is_color_enabled()

    def paint(self, token: str, text: str) -> str:
        if not self.color or not text:
            return text
        return style.paint(token, text)

    def print(self, text: str, *, token: str | None = None, end: str = "\n") -> None:
        if token and self.color:
            print(style.paint(token, text), end=end)
        else:
            print(text, end=end)

    def banner(self, version: str | None = None) -> None:
        print(style.banner(version))

    def rule(self, char: str = "─") -> None:
        print(char * 60)

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(cell)))

        def fmt_row(cells: list[str]) -> str:
            parts: list[str] = []
            for i, c in enumerate(cells):
                w = widths[i] if i < len(widths) else 0
                parts.append(str(c).ljust(w))
            return "  ".join(parts)

        print(fmt_row(headers))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            print(fmt_row(row))


_output: Output | None = None


def get_output() -> Output:
    global _output
    if _output is None:
        _output = Output()
    return _output


def set_output(o: Output) -> None:
    global _output
    _output = o
