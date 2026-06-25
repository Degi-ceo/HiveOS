"""
reporters/__init__.py — reporter registry + public dispatch.

A Reporter turns an EvalReport into output (console text, JUnit XML, HTML).
New reporters can be registered with `register_reporter(MyReporter())`. The
runner and CLI reference reporters by string name, mirroring the grader
registry — symmetric design keeps the harness easy to extend.
"""
from __future__ import annotations

from typing import IO, Protocol, runtime_checkable

from hive.evals.reporters.console import ConsoleReporter
from hive.evals.reporters.html import HTMLReporter
from hive.evals.reporters.junit_xml import JUnitXMLReporter
from hive.evals.types import EvalReport

REPORTERS: dict[str, "Reporter"] = {}


@runtime_checkable
class Reporter(Protocol):
    """Protocol that registered reporters must satisfy.

    Each reporter must have:
       - `name: str` — registry key, used by the CLI's --reporter flag
       - `write(report, stream)` — writes the report to `stream`
    """

    name: str

    def write(self, report: EvalReport, stream: IO[str]) -> None: ...


def register_reporter(reporter: Reporter) -> Reporter:
    """Add a reporter to the registry. Returns the reporter for chaining."""
    if not reporter.name:
        raise ValueError("reporter.name must be a non-empty string")
    REPORTERS[reporter.name] = reporter
    return reporter


def get_reporter(name: str) -> Reporter:
    """Look up a reporter by name. Raises KeyError on unknown names."""
    try:
        return REPORTERS[name]
    except KeyError:
        available = ", ".join(sorted(REPORTERS)) or "(none registered)"
        raise KeyError(f"unknown reporter {name!r}; available: {available}") from None


def all_reporters() -> list[Reporter]:
    return list(REPORTERS.values())


# Built-ins — register eagerly so `hive eval --reporter junit_xml` works out of the box.
register_reporter(ConsoleReporter())
register_reporter(JUnitXMLReporter())
register_reporter(HTMLReporter())


__all__ = [
    "REPORTERS",
    "Reporter",
    "all_reporters",
    "get_reporter",
    "register_reporter",
]
