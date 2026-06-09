"""
executor.py — managed agent tick with retries + normalized terminal outcome.

ADAPT/PATTERN of OpenJarvis AgentExecutor + OpenClaw agent-run-terminal-outcome
(SYNTHESIS Part B): run one agent tick with up-to-N retries (retryable errors only,
jittered backoff reused from llm.failover), and normalize the result into ONE closed
TerminalOutcome here — callers never re-derive timeout/cancel/fail precedence
(OpenClaw rule). Cancellation always propagates.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Callable

from hive.agents.base import AgentContext, AgentResult, BaseAgent
from hive.llm.failover import RetryPolicy

log = logging.getLogger("hive.agents.executor")


class TerminalOutcome(str, enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class TickResult:
    outcome: TerminalOutcome
    result: AgentResult | None = None
    attempts: int = 0
    error: str | None = None


class AgentExecutor:
    def __init__(
        self, *, retry: RetryPolicy | None = None,
        is_retryable: Callable[[Exception], bool] | None = None,
    ) -> None:
        self._retry = retry or RetryPolicy()
        # A tick crash is treated as transient by default; inject to mark fatals.
        self._is_retryable = is_retryable or (lambda _exc: True)

    async def execute_tick(
        self, agent: BaseAgent, input: str, context: AgentContext | None = None,
    ) -> TickResult:
        last_error = ""
        for attempt in range(self._retry.max_attempts):
            try:
                result = await agent.run(input, context)
                return TickResult(TerminalOutcome.COMPLETED, result=result, attempts=attempt + 1)
            except asyncio.CancelledError:
                raise  # cancellation is not a retryable agent failure
            except Exception as exc:  # noqa: BLE001 - retry decided by predicate
                last_error = str(exc)
                retryable = self._is_retryable(exc)
                log.warning("tick attempt=%s failed (retryable=%s): %s",
                            attempt + 1, retryable, exc)
                if retryable and attempt < self._retry.max_attempts - 1:
                    await asyncio.sleep(self._retry.backoff(attempt))
                    continue
                return TickResult(TerminalOutcome.FAILED, attempts=attempt + 1, error=last_error)
        return TickResult(TerminalOutcome.FAILED, attempts=self._retry.max_attempts,
                          error=last_error or "exhausted")
