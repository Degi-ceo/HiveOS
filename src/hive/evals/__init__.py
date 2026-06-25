"""
hive.evals — production-grade regression gate (Sprint 6, P-B).

Public surface:

    from hive.evals import (
        EvalItem, GraderResult, EvalResult, EvalSummary, EvalReport,
        load, load_jsonl, load_yaml, load_many,
        run, run_async, make_report,
        GRADERS, REPORTERS,
        get_grader, register_grader, get_reporter, register_reporter,
        DatasetError,
    )

See docs/sprints/SPRINT_6_AUTONOMY_LIB.md § P-B for the design contract.
"""
from __future__ import annotations

from hive.evals.dataset import (
    DatasetError,
    load,
    load_jsonl,
    load_many,
    load_yaml,
)
from hive.evals.graders import GRADERS, get_grader, register_grader
from hive.evals.reporters import REPORTERS, get_reporter, register_reporter
from hive.evals.runner import make_report, run, run_async
from hive.evals.types import (
    EvalItem,
    EvalReport,
    EvalResult,
    EvalSummary,
    GraderResult,
)

__all__ = [
    "DatasetError",
    "EvalItem",
    "EvalReport",
    "EvalResult",
    "EvalSummary",
    "GRADERS",
    "GraderResult",
    "REPORTERS",
    "get_grader",
    "get_reporter",
    "load",
    "load_jsonl",
    "load_many",
    "load_yaml",
    "make_report",
    "register_grader",
    "register_reporter",
    "run",
    "run_async",
]
