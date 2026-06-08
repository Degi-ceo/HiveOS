"""
base.py — provider adapter contract + request/result shapes.

Adapters normalize provider wire formats to/from HiveOS core types at the edge
(SYNTHESIS A.1). The router speaks only this contract; resilience (failover,
credential rotation, budget) lives in the router, not the adapter. `complete`
receives the api_key chosen by the router's credential pool.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from hive.core.types import Message


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class CompletionRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    max_tokens: int = 4_096
    thinking: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CompletionResult:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


class LLMAdapter(ABC):
    """A provider endpoint. Pure I/O — no retry/rotation/budget logic here."""

    name: str = "adapter"

    @abstractmethod
    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
