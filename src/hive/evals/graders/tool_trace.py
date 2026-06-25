"""
tool_trace.py — verify the agent invoked the right tools (and no forbidden ones).

Tool traces aren't always present in the plain string output of `HiveOS.ask()` —
they live on the orchestrator's run record. To keep this grader testable without
plumbing the full orchestrator through evals, we accept either of two inputs:

  1. The output string contains an inline "tools called: a, b, c" header that
     some surfaces emit (a fallback format, easy to fake in tests).
  2. The runner attached a real `tool_trace` list to `item.extra` when the item
     was processed. This grader reads from that list.

`extra` keys:
  - required_tools: list[str] — every entry must appear in the trace
  - forbidden_tools: list[str] — no entry may appear (anti-tool selection)
"""
from __future__ import annotations

from hive.evals.graders.base import fail, pass_
from hive.evals.types import EvalItem, GraderResult


class ToolTraceGrader:
    name = "tool_trace"

    def grade(self, item: EvalItem, output: str) -> GraderResult:
        trace: list[str] = list(item.extra.get("_trace") or _parse_inline_trace(output))
        required: list[str] = [str(t) for t in item.extra.get("required_tools", [])]
        forbidden: list[str] = [str(t) for t in item.extra.get("forbidden_tools", [])]

        missing = [t for t in required if t not in trace]
        called_forbidden = [t for t in forbidden if t in trace]

        if missing or called_forbidden:
            msg_parts = []
            if missing:
                msg_parts.append(f"missing required tools: {missing}")
            if called_forbidden:
                msg_parts.append(f"called forbidden tools: {called_forbidden}")
            return fail("; ".join(msg_parts) + f" (trace={trace})")

        # Score reflects tool precision/recall in a transparent way.
        denom = max(len(required) + len(forbidden), 1)
        score = (len(required) + len(forbidden) - len(missing) - len(called_forbidden)) / denom
        return pass_(f"trace={trace}", score=max(0.0, min(1.0, score)))


def _parse_inline_trace(output: str) -> list[str]:
    """Look for an inline "tools called: a, b, c" header — a fallback format
    emitted by some surfaces and used in tests. Returns [] if not present."""
    marker = "tools called:"
    idx = output.lower().find(marker)
    if idx < 0:
        return []
    after = output[idx + len(marker):].splitlines()[0]
    return [t.strip() for t in after.split(",") if t.strip()]


def make() -> ToolTraceGrader:
    return ToolTraceGrader()
