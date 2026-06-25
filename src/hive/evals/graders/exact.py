"""
exact.py — string-equality grader (after whitespace normalization).

The dataset's `expected` field is compared against the target output. A `case_insensitive`
flag in `extra` (default False) controls whether the comparison lowercases both sides.
Score is 1.0 on pass, 0.0 on fail — this is a binary grader.
"""
from __future__ import annotations

from hive.evals.graders.base import GraderResult, fail, normalize, pass_
from hive.evals.types import EvalItem


class ExactGrader:
    name = "exact"

    def grade(self, item: EvalItem, output: str) -> GraderResult:
        left = normalize(output)
        right = normalize(item.expected)
        if item.extra.get("case_insensitive"):
            left, right = left.lower(), right.lower()
        if left == right:
            return pass_()
        # Show a short diff-style hint without dumping entire strings.
        preview_out = output[:80] + ("…" if len(output) > 80 else "")
        preview_exp = item.expected[:80] + ("…" if len(item.expected) > 80 else "")
        return fail(f"expected {preview_exp!r}, got {preview_out!r}")


def make() -> ExactGrader:
    """Factory used by the registry — keeps construction symmetric."""
    return ExactGrader()
