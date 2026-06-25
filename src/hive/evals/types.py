"""
types.py — core dataclasses for the evals harness.

EvalItem       one row in a dataset (input + expected + grader name)
GraderResult   verdict for a single eval: score 0..1, passed bool, message
EvalResult     per-item outcome (item + output + grader result + duration)
EvalReport     aggregate of an entire dataset run (results + summary)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvalItem:
    """A single evaluation case. `extra` carries grader-specific config
    (regex pattern, tool name to trace, llm judge rubric, etc.)."""
    id: str
    input: str
    expected: str
    grader: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraderResult:
    """Outcome of grading one (item, output) pair."""
    passed: bool
    score: float  # 0.0 .. 1.0 (1.0 = perfect match)
    message: str = ""


@dataclass
class EvalResult:
    """Full record of one eval run: what we asked, what we got, what the
    grader said, and how long it took."""
    item: EvalItem
    output: str
    grader_result: GraderResult
    duration_ms: float
    error: str | None = None  # populated when the target raised

    @property
    def passed(self) -> bool:
        return self.error is None and self.grader_result.passed


@dataclass
class EvalSummary:
    """Aggregate counts for a report."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    avg_score: float = 0.0
    total_duration_ms: float = 0.0

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.errored == 0


@dataclass
class EvalReport:
    """Top-level container: dataset path, results, summary, timestamp."""
    dataset_path: str
    started_at: str  # ISO 8601 UTC
    finished_at: str  # ISO 8601 UTC
    results: list[EvalResult] = field(default_factory=list)
    summary: EvalSummary = field(default_factory=EvalSummary)

    def recompute_summary(self) -> None:
        """Recalculate summary from results — call after adding/removing results."""
        s = EvalSummary(total=len(self.results))
        total_score = 0.0
        total_ms = 0.0
        for r in self.results:
            total_ms += r.duration_ms
            if r.error is not None:
                s.errored += 1
            elif r.grader_result.passed:
                s.passed += 1
            else:
                s.failed += 1
            total_score += r.grader_result.score
        s.total_duration_ms = total_ms
        s.avg_score = (total_score / s.total) if s.total else 0.0
        self.summary = s
