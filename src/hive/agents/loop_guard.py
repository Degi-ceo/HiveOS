"""
loop_guard.py — degenerate-loop detection (OpenJarvis LoopGuard + Hermes guardrails).

TAKE of OpenJarvis loop_guard.py / Hermes tool_guardrails.py (SYNTHESIS Part B):
stops an agent that is stuck. Three cheap strategies over the tool-call stream:
identical-call repetition (same tool+args), A-B-A-B ping-pong, and a per-tool call
budget. `check()` returns a human reason string when the loop is degenerate, else
None — the orchestrator breaks the turn on a non-None result.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any


class LoopGuard:
    def __init__(self, *, max_identical: int = 3, max_per_tool: int = 10) -> None:
        self._max_identical = max_identical
        self._max_per_tool = max_per_tool
        self._hashes: list[str] = []
        self._per_tool: Counter[str] = Counter()

    @staticmethod
    def _hash(tool: str, args: dict[str, Any]) -> str:
        payload = f"{tool}:{json.dumps(args, sort_keys=True, default=str)}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def check(self, tool: str, args: dict[str, Any] | None = None) -> str | None:
        h = self._hash(tool, dict(args or {}))
        self._hashes.append(h)
        self._per_tool[tool] += 1

        if self._per_tool[tool] > self._max_per_tool:
            return f"tool '{tool}' exceeded its call budget ({self._max_per_tool})"

        recent = self._hashes[-self._max_identical:]
        if len(recent) == self._max_identical and len(set(recent)) == 1:
            return f"identical '{tool}' call repeated {self._max_identical}x"

        window = self._hashes[-4:]
        if len(window) == 4 and window[0] == window[2] and window[1] == window[3] \
                and window[0] != window[1]:
            return "ping-pong loop detected (A-B-A-B)"
        return None

    def reset(self) -> None:
        """Clear all recorded call history. Use between independent agent turns."""
        self._hashes.clear()
        self._per_tool.clear()

    def stats(self) -> dict:
        """Return a snapshot of the current loop-guard state.

        Returns total_calls, unique_tools, per_tool counts, and max limits."""
        return {
            "total_calls": len(self._hashes),
            "unique_tools": len(self._per_tool),
            "per_tool": dict(self._per_tool),
            "max_identical": self._max_identical,
            "max_per_tool": self._max_per_tool,
        }
