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
from hive.llm.pricing import cost_usd

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


# PlannerError lives with the Codex adapter now (one implementation); re-exported here
# so existing callers/tests (`from hive.llm.router import PlannerError`) keep working.
from hive.llm.adapters.codex import CodexCommand, PlannerError, render_prompt, run_codex  # noqa: E402

# (ok, reason) — True allows the call. None gate => always allow.
BudgetGate = Callable[[], tuple[bool, str]]
Planner = Callable[[list[Message], str | None], Awaitable[str]]


def make_codex_planner(cmd: CodexCommand, *, timeout: float = 120.0) -> Planner:
    """Headless Codex (ChatGPT Plus OAuth) planner. Thinking only — no execution.

    Thin wrapper over the shared `run_codex` subprocess core (llm/adapters/codex.py):
    stdin-fed, timeout-bounded, raises PlannerError so ModelRouter.complete falls back
    to the executor instead of dead-ending a turn."""
    async def plan(messages: list[Message], system: str | None) -> str:
        return await run_codex(cmd, render_prompt(messages, system), timeout=timeout)

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
            self._planner = make_codex_planner(cfg.planner_cmd, timeout=cfg.planner_timeout)
        else:
            self._planner = None
        # Near-exhaustion threshold: cool a credential when its hottest rate-limit
        # window is at/above this percent, so the next call rotates before a 429.
        self._cooldown_pct = 90.0

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
        tools: list[dict] | None = None,
        **extra: object,
    ) -> CompletionResult:
        if kind is TaskKind.PLAN and self._planner is not None:
            try:
                text = await self._planner(messages, system)
                return CompletionResult(text=text, model="planner", finish_reason="stop")
            except PlannerError as exc:
                # A broken Codex login must never dead-end a turn: log and fall
                # through to the executor (MiniMax) as the planner of last resort.
                log.warning("codex planner failed, falling back to executor: %s", exc)

        ok, why = self._budget() if self._budget else (True, "")
        if not ok:
            self._emit(EventType.BUDGET_BLOCK, reason=why)
            raise BudgetError(why)

        base = CompletionRequest(
            model="", messages=messages, system=system,
            max_tokens=max_tokens, thinking=thinking, tools=tools, extra=dict(extra),
        )

        last_exc: Exception | None = None
        last_ce = None
        for model in self._model_chain(kind):
            request = replace(base, model=model)
            for attempt in range(self._retry.max_attempts):
                cred = self._pool.acquire()
                if cred is None:
                    if len(self._pool) == 0:
                        raise NoCredentialsError("no API key configured for the executor")
                    # The pool emptied because we just cooled the failing key — surface the
                    # real cause (e.g. auth/billing), not a generic "all cooling".
                    if last_ce is not None:
                        raise ProviderError(f"{last_ce.reason.value}: {last_ce.detail}") from last_exc
                    raise NoCredentialsError("all credentials are in cooldown")
                self._emit(EventType.INFERENCE_START, model=model, attempt=attempt)
                try:
                    result = await self._adapter.complete(request, api_key=cred.key)
                except Exception as exc:  # noqa: BLE001 - classified below
                    ce = classify(exc)
                    last_exc = exc
                    last_ce = ce
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
                    self._maybe_cool_for_rate_limit(cred, result)
                    # Cost is computed HERE (llm layer owns pricing); the budgeter in
                    # core only accumulates it, so core stays a DAG leaf.
                    self._emit(EventType.INFERENCE_END, model=model,
                               input_tokens=result.usage.input_tokens,
                               output_tokens=result.usage.output_tokens,
                               cost_usd=cost_usd(model, result.usage.input_tokens,
                                                 result.usage.output_tokens))
                    return result

        raise ProviderError("all models and attempts exhausted") from last_exc

    async def stream(self, messages: list[Message], *, system: str | None = None,
                     max_tokens: int = 4_096, thinking: bool = False, **extra: object):
        """Stream executor text deltas (for conversational surfaces, e.g. SSE).

        Budget-gated; single model + single credential (no mid-stream failover — a
        stream can't be retried once bytes are sent). Emits INFERENCE_END with the
        streamed length so telemetry/budget still see the call. Tools are not used on
        the streaming path; the agentic tool loop stays on complete()."""
        ok, why = self._budget() if self._budget else (True, "")
        if not ok:
            self._emit(EventType.BUDGET_BLOCK, reason=why)
            raise BudgetError(why)
        cred = self._pool.acquire()
        if cred is None:
            if len(self._pool) == 0:
                raise NoCredentialsError("no API key configured for the executor")
            raise NoCredentialsError("all credentials are in cooldown")

        model = self._model_chain(TaskKind.EXECUTE)[0]
        request = CompletionRequest(model=model, messages=messages, system=system,
                                    max_tokens=max_tokens, thinking=thinking,
                                    extra=dict(extra))
        self._emit(EventType.INFERENCE_START, model=model, attempt=0)
        chars = 0
        try:
            async for delta in self._adapter.astream(request, api_key=cred.key):
                chars += len(delta)
                yield delta
        except Exception as exc:  # noqa: BLE001
            self._pool.report_failure(cred)
            raise ProviderError(f"{classify(exc).reason.value}: stream failed") from exc
        self._pool.report_success(cred)
        # Rough output-token estimate from streamed chars (~4 chars/token) so the
        # budgeter still records streamed usage.
        est_out = max(1, chars // 4)
        self._emit(EventType.INFERENCE_END, model=model, input_tokens=0,
                   output_tokens=est_out, cost_usd=cost_usd(model, 0, est_out))

    def _maybe_cool_for_rate_limit(self, cred, result: CompletionResult) -> None:
        """Proactively park a credential whose rate-limit window is nearly spent.

        The adapter attaches a parsed RateLimitState to result.raw when the provider
        sends x-ratelimit-* headers. If the hottest window is at/above the threshold,
        cool this key until that window resets so the next call rotates to a fresh key
        instead of taking a 429 (cheaper than reactive failover)."""
        state = result.raw.get("rate_limit_state")
        if state is None:
            return
        hottest = state.hottest()
        if hottest is None or hottest.usage_pct < self._cooldown_pct:
            return
        seconds = max(1.0, hottest.remaining_seconds_now)
        log.info("cooling credential %s for %.0fs (rate-limit %.0f%%)",
                 cred.label, seconds, hottest.usage_pct)
        # cooldown() (not report_failure) — this key is healthy, just rate-limited.
        self._pool.cooldown(cred, seconds)

    async def aclose(self) -> None:
        await self._adapter.aclose()

    def status(self) -> dict:
        """Return a snapshot of the router's current configuration and pool state.

        Safe to call without a live model — reads only cached local state."""
        pool_status = []
        try:
            pool_status = self._pool.status()
        except Exception:  # noqa: BLE001
            pass
        return {
            "exec_model": self._cfg.exec_model,
            "exec_fallback_model": self._cfg.exec_fallback_model,
            "aux_model": self._cfg.aux_model,
            "exec_provider": self._cfg.exec_provider,
            "planner_enabled": self._planner is not None,
            "pool_size": len(self._pool),
            "pool_available": self._pool.available_count(),
            "pool_status": pool_status,
        }
