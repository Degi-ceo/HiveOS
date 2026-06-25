"""
console.py — pretty-printer for eval results (always-on, default reporter).

Renders to a `TextIO` so tests can capture into `io.StringIO`. Uses ANSI color
when the stream is a TTY and `NO_COLOR` is unset; falls back to plain text
otherwise. Output is one line per item plus a summary block.
"""
from __future__ import annotations

import os
import sys
from typing import IO, TextIO

from hive.evals.types import EvalReport, GraderResult

_NAME = "console"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _use_color() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _use_color() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _use_color() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _use_color() else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _use_color() else s


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def render(report: EvalReport, stream: TextIO | None = None) -> None:
    """Print the report to `stream` (default: sys.stdout). Always available;
    used both interactively and in CI logs because it always exits cleanly
    even when stdout is redirected."""
    out: TextIO = stream if stream is not None else sys.stdout
    _write_header(report, out)
    for r in report.results:
        _write_row(r.item.id, r.passed, r.error, r.grader_result, r.duration_ms, out)
    _write_summary(report, out)


def _write_header(report: EvalReport, out: TextIO) -> None:
    out.write(_bold(f"Eval report: {report.dataset_path}\n"))
    out.write(_dim(f"  started:  {report.started_at}\n"))
    out.write(_dim(f"  finished: {report.finished_at}\n"))
    out.write("\n")


def _write_row(item_id: str, passed: bool, error: str | None,
               grader: GraderResult, ms: float, out: TextIO) -> None:
    if error is not None:
        marker = _red("ERROR")
        msg = error
    elif passed:
        marker = _green("PASS")
        msg = grader.message or ""
    else:
        marker = _yellow("FAIL")
        msg = grader.message
    out.write(f"  {marker}  {item_id:<24}  {ms:>7.1f} ms  {_dim(msg)}\n")


def _write_summary(report: EvalReport, out: TextIO) -> None:
    s = report.summary
    out.write("\n")
    out.write(_bold("Summary\n"))
    out.write(f"  total:     {s.total}\n")
    out.write(f"  passed:    {_green(str(s.passed))}\n")
    if s.failed:
        out.write(f"  failed:    {_red(str(s.failed))}\n")
    else:
        out.write(f"  failed:    {s.failed}\n")
    if s.errored:
        out.write(f"  errored:   {_red(str(s.errored))}\n")
    else:
        out.write(f"  errored:   {s.errored}\n")
    out.write(f"  pass rate: {s.pass_rate * 100:.1f}%\n")
    out.write(f"  avg score: {s.avg_score:.3f}\n")
    out.write(f"  duration:  {s.total_duration_ms:.1f} ms\n")
    if s.all_passed:
        out.write("\n" + _green(_bold("ALL PASSED\n")))
    else:
        out.write("\n" + _red(_bold("EVALS FAILED\n")))


def make() -> "ConsoleReporter":
    """Factory used by the registry."""
    return ConsoleReporter()


class ConsoleReporter:
    name = _NAME

    def write(self, report: EvalReport, stream: IO[str] | None = None) -> None:
        render(report, stream=stream)  # type: ignore[arg-type]
