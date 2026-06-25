"""
base.py — Grader protocol and shared helpers.

A Grader takes (item, output) and returns a GraderResult. Implementations live
in exact.py, regex.py, llm_judge.py, tool_trace.py. The registry in
graders/__init__.py maps string names → grader instances.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hive.evals.types import EvalItem, GraderResult


@runtime_checkable
class Grader(Protocol):
    """Anything with a `name` class attribute and a `grade(item, output)` method.

    Use `register_grader()` from graders/__init__.py to add new ones; the
    string `name` is what datasets reference in their `grader:` field."""

    name: str

    def grade(self, item: EvalItem, output: str) -> GraderResult: ...


def fail(message: str, score: float = 0.0) -> GraderResult:
    """Shorthand for a non-passing GraderResult with a human message."""
    return GraderResult(passed=False, score=score, message=message)


def pass_(message: str = "", score: float = 1.0) -> GraderResult:
    """Shorthand for a passing GraderResult. Empty message means clean pass."""
    return GraderResult(passed=True, score=score, message=message)


def normalize(text: str) -> str:
    """Trim and collapse whitespace — graders that do exact/regex compare use
    this for stable comparisons across OS line endings and stray spaces."""
    return " ".join(text.split())
