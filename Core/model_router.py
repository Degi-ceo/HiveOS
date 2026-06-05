"""
model_router.py — HiveOS model routing.

Two roles, per the researched planner/executor split:
  - EXECUTOR  -> MiniMax (Anthropic-compatible endpoint, interleaved thinking).
                 Used for all implementation, edits, tests, search, memory work.
  - PLANNER   -> ChatGPT Plus via Codex OAuth (headless `codex exec`).
                 Used ONLY for heavy planning/architecture/gap-analysis. Never executes.

Routing is by TaskKind. Aux tasks use the cheap/standard exec model and never
invoke the planner (quota preservation). Every executor call passes the budgeter.

Uses the Anthropic /v1/messages shape against MiniMax's Anthropic endpoint so
`thinking` blocks are preserved across turns (required for M2 performance).
"""
from __future__ import annotations
import enum
import asyncio
import logging
import shlex
import httpx

from core import settings
from core.budgeter import budgeter

log = logging.getLogger("hiveos.router")


class TaskKind(enum.Enum):
    EXECUTE = "execute"     # implement / edit / test / search  -> MiniMax
    AUX = "aux"             # summaries / memory / classify      -> MiniMax (cheap)
    PLAN = "plan"           # heavy planning / architecture      -> ChatGPT Plus


class ModelRouter:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=300)

    async def complete(
        self,
        messages: list[dict],
        kind: TaskKind = TaskKind.EXECUTE,
        system: str | None = None,
        max_tokens: int = 4096,
        thinking: bool = True,
        **kw,
    ) -> str:
        if kind is TaskKind.PLAN and settings.PLANNER_ENABLED:
            return await self._plan(messages, system)
        # PLAN falls back to executor if planner disabled.
        model = settings.AUX_MODEL if kind is TaskKind.AUX else settings.EXEC_MODEL
        ok, why = await budgeter.can_spend()
        if not ok:
            raise RuntimeError(f"budget block: {why}")
        try:
            out = await self._minimax(model, messages, system, max_tokens, thinking, **kw)
            budgeter.record_call()
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("exec model %s failed: %s; trying fallback", model, e)
            out = await self._minimax(
                settings.EXEC_FALLBACK_MODEL, messages, system, max_tokens, thinking, **kw
            )
            budgeter.record_call()
            return out

    async def _minimax(
        self, model: str, messages: list[dict], system: str | None,
        max_tokens: int, thinking: bool, **kw,
    ) -> str:
        """Anthropic-compatible Messages API against MiniMax."""
        body: dict = {"model": model, "max_tokens": max_tokens, "messages": messages, **kw}
        if system:
            body["system"] = system
        if thinking:
            # interleaved thinking; preserved across turns by the caller
            body["thinking"] = {"type": "enabled", "budget_tokens": 2048}
        r = await self._client.post(
            f"{settings.MINIMAX_ANTHROPIC_BASE}/v1/messages",
            headers={
                "x-api-key": settings.MINIMAX_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        # Concatenate text blocks; ignore thinking blocks in the returned string.
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()

    async def _plan(self, messages: list[dict], system: str | None) -> str:
        """Heavy planning via headless Codex (ChatGPT Plus OAuth). Thinking only."""
        prompt = ""
        if system:
            prompt += f"[CONTEXT]\n{system}\n\n"
        prompt += "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        cmd = f'{settings.PLANNER_CMD} {shlex.quote(prompt)}'
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return out.decode().strip()

    async def aclose(self) -> None:
        await self._client.aclose()


router = ModelRouter()
