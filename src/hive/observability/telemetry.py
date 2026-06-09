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
    tool_calls: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    def attach(self, bus: EventBus) -> "Telemetry":
        bus.subscribe(EventType.INFERENCE_END, self._on_inference)
        bus.subscribe(EventType.TOOL_CALL_END, self._on_tool)
        return self

    def _on_inference(self, event: Any) -> None:
        data = _data(event)
        self.inference_calls += 1
        self.output_tokens += int(data.get("output_tokens", 0) or 0)
        model = str(data.get("model", "?"))
        self.by_model[model] = self.by_model.get(model, 0) + 1

    def _on_tool(self, event: Any) -> None:
        self.tool_calls += 1

    def snapshot(self) -> dict:
        return {"inference_calls": self.inference_calls, "output_tokens": self.output_tokens,
                "tool_calls": self.tool_calls, "by_model": dict(self.by_model)}


def _data(event: Any) -> dict:
    """EventBus may deliver an Event object or a raw dict; accept both."""
    return getattr(event, "data", event) or {}
