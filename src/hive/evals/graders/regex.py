"""
regex.py — pattern-match grader.

The dataset's `expected` field is treated as the regex pattern (or, if `extra.pattern`
is set, that takes precedence). Flags can be controlled via `extra.flags` (an int,
defaults to 0). Score is 1.0 on full match, 0.0 on no match — partial credit is not
supported yet (use a separate `llm_judge` for graded scoring).
"""
from __future__ import annotations

import re
from typing import Any

from hive.evals.graders.base import GraderResult, fail, pass_
from hive.evals.types import EvalItem


class RegexGrader:
    name = "regex"

    def grade(self, item: EvalItem, output: str) -> GraderResult:
        pattern = str(item.extra.get("pattern") or item.expected)
        flags = int(_coerce_flags(item.extra.get("flags", 0)))
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            return fail(f"invalid regex {pattern!r}: {e}")
        m = rx.search(output)
        if m is None:
            return fail(f"pattern {pattern!r} did not match output")
        # Full-string match is the strongest signal; record it in the message.
        if rx.fullmatch(output) is not None:
            return pass_(f"full match at {m.span()}")
        return pass_(f"matched at {m.span()} (substring)")


def _coerce_flags(value: Any) -> int:
    """Accept either an int or a comma-separated string of flag names so YAML/JSON
    datasets stay readable. Unknown names are silently ignored — pytest output
    for invalid flags should be a hard fail, not a soft one."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        mapping = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}
        out = 0
        for name in value.split(","):
            n = name.strip().lower()
            if n in mapping:
                out |= mapping[n]
        return out
    return 0


def make() -> RegexGrader:
    return RegexGrader()
