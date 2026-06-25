"""
llm_judge.py — model-graded rubric scorer (interface only).

This module is the **interface contract** for future LLM-as-judge support. The
Sprint 6 scope explicitly defers the actual model call (issue #70 says
"requires paid judge model — deferred, needs budget approval"). Until then this
grader falls back to a deterministic heuristic:

  * If the target output contains the expected text (after normalization), pass with 1.0
  * Otherwise, fail with a message explaining the heuristic

The shape of `grade()` is final — wiring a real judge later only swaps the body.
Datasets can already declare `grader: llm_judge` and `extra.rubric` and `extra.threshold`
without code changes.
"""
from __future__ import annotations

from hive.evals.graders.base import fail, normalize
from hive.evals.types import EvalItem, GraderResult

_DEFAULT_THRESHOLD = 0.7


class LLMJudgeGrader:
    name = "llm_judge"

    def grade(self, item: EvalItem, output: str) -> GraderResult:
        # Deterministic heuristic until a budget-approved judge model is wired.
        # The score mirrors the heuristic (0.0 or 1.0); real model judges will
        # return fractional scores and compare against item.extra["threshold"].
        threshold = float(item.extra.get("threshold", _DEFAULT_THRESHOLD))
        rubric = str(item.extra.get("rubric", ""))
        if normalize(item.expected).lower() in normalize(output).lower():
            score = 1.0
        else:
            score = 0.0
        passed = score >= threshold
        msg_parts = []
        if rubric:
            msg_parts.append(f"rubric={rubric!r}")
        msg_parts.append(f"score={score:.2f} threshold={threshold:.2f}")
        if passed:
            return GraderResult(passed=True, score=score, message="; ".join(msg_parts))
        return fail(
            f"heuristic judge: score {score:.2f} below threshold {threshold:.2f}"
        )


def make() -> LLMJudgeGrader:
    return LLMJudgeGrader()
