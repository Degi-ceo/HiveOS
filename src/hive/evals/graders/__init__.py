"""
graders/__init__.py — grader registry + public dispatch.

Datasets reference graders by string name (e.g. `grader: exact`). Built-in
graders are registered at import time; user code can register more via
`register_grader(MyGrader())`. Unknown grader names raise `KeyError` from
`get_grader()` — fail-fast on typos so dataset authors see the mistake
immediately rather than in CI.
"""
from __future__ import annotations

from typing import Iterable

from hive.evals.graders.base import Grader, GraderResult, fail, pass_
from hive.evals.graders.exact import ExactGrader
from hive.evals.graders.llm_judge import LLMJudgeGrader
from hive.evals.graders.regex import RegexGrader
from hive.evals.graders.tool_trace import ToolTraceGrader

GRADERS: dict[str, Grader] = {}


def register_grader(grader: Grader) -> Grader:
    """Add a grader to the registry. Returns the grader for chaining."""
    if not grader.name:
        raise ValueError("grader.name must be a non-empty string")
    GRADERS[grader.name] = grader
    return grader


def get_grader(name: str) -> Grader:
    """Look up a grader by name. Raises KeyError on unknown names — fail-fast."""
    try:
        return GRADERS[name]
    except KeyError:
        available = ", ".join(sorted(GRADERS)) or "(none registered)"
        raise KeyError(f"unknown grader {name!r}; available: {available}") from None


def all_graders() -> Iterable[Grader]:
    """Iterate registered graders — useful for tests that introspect the registry."""
    return GRADERS.values()


# Built-ins — register eagerly so importing this module is enough to run a dataset.
register_grader(ExactGrader())
register_grader(RegexGrader())
register_grader(LLMJudgeGrader())
register_grader(ToolTraceGrader())


__all__ = [
    "Grader",
    "GRADERS",
    "GraderResult",
    "all_graders",
    "fail",
    "get_grader",
    "pass_",
    "register_grader",
]
