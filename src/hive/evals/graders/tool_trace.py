"""
tool_trace.py — verify the agent invoked the right tools (and no forbidden ones).

Tool traces are accepted only from structured runtime evidence attached by the
runner. Plain assistant text is deliberately ignored because it is untrusted
and can claim that a tool ran when no dispatch occurred.

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
        trace: list[str] = list(item.extra.get("_trace") or [])
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
def make() -> ToolTraceGrader:
    return ToolTraceGrader()
