"""
router.py — planner/executor split with one resilience decision tree.

Successor to the old Core/model_router (KEEP shape + add resilience, SYNTHESIS P2):
  - EXECUTOR -> MiniMax (Anthropic endpoint, interleaved thinking) for all
    implement/edit/test/search/memory work.
  - PLANNER  -> ChatGPT Plus via Codex OAuth (`codex exec`), heavy planning only,
    never executes; falls back to the executor when disabled.

Resilience is centralized here (not in the adapter): every executor call goes
through failover.classify -> {retry w/ jittered backoff | rotate credential |
fall back to the next model | abort}, with an injectable budget gate. All
collaborators are injectable so the loop is unit-testable without a network.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import shlex
from dataclasses import replace
from typing import Awaitable, Callable

from hive.core.config import HiveConfig, get_config
from hive.core.events import EventBus, EventType
from hive.core.types import Message
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter
from hive.llm.adapters.minimax import MiniMaxAdapter
from hive.llm.credential_pool import CredentialPool
from hive.llm.failover import RetryPolicy, classify
from hive.llm.model_catalog import ModelCatalog

log = logging.getLogger("hive.router")


class TaskKind(enum.Enum):
    EXECUTE = "execute"   # implement / edit / test / search -> MiniMax
    AUX = "aux"           # summaries / memory / classify     -> MiniMax (cheap)
    PLAN = "plan"         # heavy planning / architecture      -> ChatGPT Plus


class BudgetError(RuntimeError):
    """Raised when the budget gate blocks a call before it is attempted."""


class NoCredentialsError(RuntimeError):
    """No API key configured, or every key is in cooldown."""


class ProviderError(RuntimeError):
    """All models/attempts were exhausted."""


# (ok, reason) — True allows the call. None gate => always allow.
BudgetGate = Callable[[], tuple[bool, str]]
Planner = Callable[[list[Message], str | None], Awaitable[str]]


def make_codex_planner(cmd: str) -> Planner:
    """Headless Codex (ChatGPT Plus OAuth) planner. Thinking only — no execution."""

    async def plan(messages: list[Message], system: str | None) -> str:
        prompt = f"[CONTEXT]\n{system}\n\n" if system else ""
        prompt += "\n".join(f"{m.role.value}: {m.content}" for m in messages)
        proc = await asyncio.create_subprocess_shell(
            f"{cmd} {shlex.quote(prompt)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return out.decode().strip()

    return plan


class ModelRouter:
    def __init__(
        self,
        *,
        config: HiveConfig | None = None,
        adapter: LLMAdapter | None = None,
        credential_pool: CredentialPool | None = None,
        catalog: ModelCatalog | None = None,
        retry: RetryPolicy | None = None,
        budget: BudgetGate | None = None,
        planner: Planner | None = None,
        events: EventBus | None = None,
    ) -> None:
        cfg = config or get_config()
        self._cfg = cfg
        self._catalog = catalog or ModelCatalog()
        self._adapter = adapter or MiniMaxAdapter(cfg.minimax_anthropic_base, self._catalog)
        self._pool = credential_pool or CredentialPool([cfg.minimax_api_key])
        self._retry = retry or RetryPolicy()
        self._budget = budget
        self._events = events
        if planner is not None:
            self._planner: Planner | None = planner
        elif cfg.planner_enabled:
            self._planner = make_codex_planner(cfg.planner_cmd)
        else:
            self._planner = None

    def _model_chain(self, kind: TaskKind) -> list[str]:
        if kind is TaskKind.AUX:
            return [self._cfg.aux_model]
        chain = [self._cfg.exec_model]
        if self._cfg.exec_fallback_model not in chain:
            chain.append(self._cfg.exec_fallback_model)
        return chain

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.publish(event_type, dict(data))

    async def complete(
        self,
        messages: list[Message],
        kind: TaskKind = TaskKind.EXECUTE,
        *,
        system: str | None = None,
        max_tokens: int = 4_096,
        thinking: bool = True,
        **extra: object,
    ) -> CompletionResult:
        if kind is TaskKind.PLAN and self._planner is not None:
            text = await self._planner(messages, system)
            return CompletionResult(text=text, model="planner", finish_reason="stop")

        ok, why = self._budget() if self._budget else (True, "")
        if not ok:
            raise BudgetError(why)

        base = CompletionRequest(
            model="", messages=messages, system=system,
            max_tokens=max_tokens, thinking=thinking, extra=dict(extra),
        )

        last_exc: Exception | None = None
        for model in self._model_chain(kind):
            request = replace(base, model=model)
            for attempt in range(self._retry.max_attempts):
                cred = self._pool.acquire()
                if cred is None:
                    if len(self._pool) == 0:
                        raise NoCredentialsError("no API key configured for the executor")
                    raise NoCredentialsError("all credentials are in cooldown")
                self._emit(EventType.INFERENCE_START, model=model, attempt=attempt)
                try:
                    result = await self._adapter.complete(request, api_key=cred.key)
                except Exception as exc:  # noqa: BLE001 - classified below
                    ce = classify(exc)
                    last_exc = exc
                    log.warning("model=%s attempt=%s failed: %s", model, attempt, ce.reason.value)
                    if ce.should_rotate_credential:
                        self._pool.report_failure(cred)
                    if ce.retryable and attempt < self._retry.max_attempts - 1:
                        await asyncio.sleep(self._retry.backoff(attempt))
                        continue
                    if ce.should_fallback:
                        break  # try the next model in the chain
                    raise ProviderError(f"{ce.reason.value}: {ce.detail}") from exc
                else:
                    self._pool.report_success(cred)
                    self._emit(EventType.INFERENCE_END, model=model,
                               output_tokens=result.usage.output_tokens)
                    return result

        raise ProviderError("all models and attempts exhausted") from last_exc

    async def aclose(self) -> None:
        await self._adapter.aclose()
