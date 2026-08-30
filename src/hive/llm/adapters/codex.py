"""
codex.py — Codex (ChatGPT Plus via OAuth) as a normalized LLMAdapter (M8 B1).

Ports the spirit of Hermes's codex adapter to HiveOS: wrap the headless `codex exec`
subprocess behind the LLMAdapter contract so the planner output is a normal
CompletionResult, hardened the same way the router's planner already was (stdin, not
argv; timeout-bounded; typed PlannerError on failure). `run_codex` is the single shared
subprocess core — both CodexAdapter and the router's make_codex_planner use it, so there
is one implementation, one set of failure semantics.

Codex authenticates via the local OAuth session, not an api_key, so `complete(api_key=)`
is accepted and ignored (it does not participate in the credential pool).
"""
from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Sequence

from hive.core.types import Message
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter


class PlannerError(RuntimeError):
    """The Codex subprocess failed, timed out, returned nothing, or wasn't launchable."""


def render_prompt(messages: list[Message], system: str | None) -> str:
    """Flatten a chat into a single prompt string for the headless CLI."""
    prefix = f"[CONTEXT]\n{system}\n\n" if system else ""
    return prefix + "\n".join(f"{m.role.value}: {m.content}" for m in messages)


CodexCommand = str | Sequence[str]

async def run_codex(cmd: CodexCommand, prompt: str, *, timeout: float = 120.0) -> str:
    """Run `cmd` (e.g. "codex exec") with the prompt on stdin; return stdout text.

    Raises PlannerError on non-zero exit / empty output / missing binary / timeout.
    The prompt is never interpolated into the command line (no argv length/injection)."""
    argv = shlex.split(cmd, posix=os.name != "nt") if isinstance(cmd, str) else list(cmd)
    if not argv:
        raise PlannerError("codex command is empty")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        raise PlannerError(f"codex not launchable ({cmd}): {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.wait()  # reap so the transport closes cleanly
        except ProcessLookupError:
            pass
        raise PlannerError(f"codex timed out after {timeout}s") from exc
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise PlannerError(f"codex exited {proc.returncode}: {text[-300:]}")
    if not text:
        raise PlannerError("codex returned no output")
    return text


class CodexAdapter(LLMAdapter):
    """Codex (ChatGPT Plus) behind the adapter contract — planning, never execution."""

    name = "codex"

    def __init__(self, cmd: CodexCommand = "codex exec", *, timeout: float = 120.0) -> None:
        self._cmd = cmd
        self._timeout = timeout

    async def complete(self, request: CompletionRequest, *, api_key: str = "") -> CompletionResult:
        text = await run_codex(self._cmd, render_prompt(request.messages, request.system),
                               timeout=self._timeout)
        return CompletionResult(text=text, model="codex", finish_reason="stop")
