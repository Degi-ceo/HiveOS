"""
telemetry.py — counters aggregated off the EventBus (ADAPT OpenJarvis telemetry).

Observability SUBSCRIBES to the bus; producers never call it (no reverse coupling,
SYNTHESIS DAG). Counts model calls + output tokens (INFERENCE_END) and tool calls
(TOOL_CALL_END). Subscribers must be fast/non-blocking (EventBus contract).

Depends on hive.core only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hive.core.events import EventBus, EventType


@dataclass(slots=True)
class Telemetry:
    inference_calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)
    cost_by_model: dict[str, float] = field(default_factory=dict)
    tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    # Self-modification counters
    selfmod_attempts: int = 0
    selfmod_succeeded: int = 0
    selfmod_failed: int = 0

    def attach(self, bus: EventBus) -> "Telemetry":
        bus.subscribe(EventType.INFERENCE_END, self._on_inference)
        bus.subscribe(EventType.TOOL_CALL_END, self._on_tool)
        bus.subscribe(EventType.SELFMOD_END, self._on_selfmod)
        return self

    def _on_inference(self, event: Any) -> None:
        data = _data(event)
        self.inference_calls += 1
        self.output_tokens += int(data.get("output_tokens", 0) or 0)
        self.input_tokens += int(data.get("input_tokens", 0) or 0)
        cost = float(data.get("cost_usd", 0.0) or 0.0)
        self.cost_usd += cost
        model = str(data.get("model", "?"))
        self.by_model[model] = self.by_model.get(model, 0) + 1
        if cost:
            self.cost_by_model[model] = self.cost_by_model.get(model, 0.0) + cost
        entry = self.tokens_by_model.setdefault(model, {"input": 0, "output": 0})
        entry["input"] += int(data.get("input_tokens", 0) or 0)
        entry["output"] += int(data.get("output_tokens", 0) or 0)

    def _on_tool(self, event: Any) -> None:
        self.tool_calls += 1

    def _on_selfmod(self, event: Any) -> None:
        data = _data(event)
        self.selfmod_attempts += 1
        if data.get("ok"):
            self.selfmod_succeeded += 1
        else:
            self.selfmod_failed += 1

    def snapshot(self) -> dict:
        return {"inference_calls": self.inference_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "tool_calls": self.tool_calls, "cost_usd": round(self.cost_usd, 6),
                "by_model": dict(self.by_model),
                "cost_by_model": {m: round(c, 6) for m, c in self.cost_by_model.items()},
                "tokens_by_model": {m: dict(t) for m, t in self.tokens_by_model.items()},
                "selfmod_attempts": self.selfmod_attempts,
                "selfmod_succeeded": self.selfmod_succeeded,
                "selfmod_failed": self.selfmod_failed}


def _data(event: Any) -> dict:
    """EventBus may deliver an Event object or a raw dict; accept both."""
    return getattr(event, "data", event) or {}
