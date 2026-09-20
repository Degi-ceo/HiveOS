"""Supervisor-owned execution boundary for credential-free local workers."""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hive.agents.base import AgentResult
from hive.agents.profiles import scoped_specialist_tools, specialist_profile
from hive.agents.worker_process import WorkerProcessController
from hive.agents.worker_protocol import WorkerProtocolError, WorkerRequest, decode, encode
from hive.context.prompt_builder import system_prompt
from hive.core.child_env import minimal_worker_environment
from hive.core.run_context import bind_delegation_id, bind_run_id
from hive.core.types import ContentEnvelope, Message, Role, ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.tools.base import BaseTool
from hive.tools.executor import DispatchStatus, ToolExecutor


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    content: str
    outcome: str
    turns: int


@dataclass(slots=True)
class _TrustedTurnState:
    """Conversation state that only the credential-owning parent may mutate."""
    messages: list[Message]
    pending: dict[str, ToolCall]
    model_calls: int = 0
    tool_calls: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = {}


class LocalWorkerSupervisor:
    """Run one profile-scoped specialist in a credential-free subprocess.

    The child owns only the turn loop.  This process owns model calls, tools,
    approvals, audit and candidate-broker state, so the child cannot acquire a
    capability by reading its environment or importing runtime closures.
    """

    def __init__(self, router: Any, tools: Mapping[str, BaseTool], *, timeout: float = 120.0,
                 max_iterations: int = 30, max_per_tool: int = 50,
                 executable: str | None = None, events: Any = None, audit: Any = None,
                 tracer: Any = None, tool_timeout: float | None = 60.0,
                 isolation_mode: str = "preferred", sandbox_mode: str = "off",
                 sandbox_image: str = "", delegation_ledger: Any = None) -> None:
        self._router = router
        self._tools = dict(tools)
        self._timeout = max(1.0, timeout)
        self._max_iterations = max(1, min(max_iterations, 30))
        self._max_per_tool = max(1, min(max_per_tool, 50))
        self._executable = executable or sys.executable
        self._source_root = str(Path(__file__).resolve().parents[2])
        self._events, self._audit, self._tracer, self._tool_timeout = (
            events, audit, tracer, tool_timeout,
        )
        self._isolation_mode = isolation_mode
        self._sandbox_mode = sandbox_mode
        self._sandbox_image = sandbox_image
        self._delegation_ledger = delegation_ledger

    async def execute(self, task: str, role: str, *, run_id: str, delegation_id: str = "",
                      max_iterations: int | None = None, max_per_tool: int | None = None,
                      timeout: float | None = None,
                      granted_tools: frozenset[str] | None = None, capability_id: str = "",
                      attempt: int = 0) -> AgentResult:
        profile = specialist_profile(role)
        allowed_tools = profile.allowed_tools if granted_tools is None else (
            profile.allowed_tools & frozenset(granted_tools)
        )
        scoped = {
            name: tool for name, tool in scoped_specialist_tools(profile.name, self._tools).items()
            if name in allowed_tools
        }
        effective_iterations = self._max_iterations if max_iterations is None else min(
            self._max_iterations, max(1, int(max_iterations)),
        )
        effective_per_tool = self._max_per_tool if max_per_tool is None else min(
            self._max_per_tool, max(1, int(max_per_tool)),
        )
        effective_timeout = self._timeout if timeout is None else min(
            self._timeout, max(1.0, float(timeout)),
        )
        request = WorkerRequest(
            role=profile.name, task=str(task), run_id=str(run_id),
            delegation_id=str(delegation_id), max_iterations=effective_iterations,
            max_per_tool=effective_per_tool, capability_id=str(capability_id),
        )
        start = request.to_dict()
        start["tools"] = [
            {"name": tool.spec.name, "description": tool.spec.description,
             "input_schema": tool.spec.parameters or {"type": "object", "properties": {}}}
            for tool in scoped.values() if tool.available()
        ]
        # Validate the whole request before a child exists, so a bad frame
        # cannot leave a process waiting forever for stdin.
        start_frame = encode(start)
        sandbox = None
        if self._sandbox_mode != "off":
            try:
                from hive.agents.worker_sandbox import DockerWorkerSandbox
                sandbox = DockerWorkerSandbox(self._sandbox_image, self._source_root)
            except Exception:
                if self._sandbox_mode == "required":
                    return AgentResult(content="[subagent failed: worker unavailable]")
        controller = WorkerProcessController(
            self._isolation_mode, sandbox=sandbox, sandbox_mode=self._sandbox_mode,
        )
        proc: asyncio.subprocess.Process | None = None
        state = _TrustedTurnState(messages=[Message(role=Role.USER, content=request.task)], pending={})
        executor = ToolExecutor(scoped, events=self._events, audit=self._audit,
                                tracer=self._tracer, timeout=self._tool_timeout)
        try:
            proc = await controller.start(
                self._executable, "-I", "-c",
                "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
                "runpy.run_module('hive.agents.worker', run_name='__main__')",
                self._source_root,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                # The protocol has its own safe error codes.  Never buffer child
                # tracebacks (which can include local paths or configuration) in
                # the supervisor, and prevent a noisy child from blocking on stderr.
                stderr=asyncio.subprocess.DEVNULL, env=minimal_worker_environment(),
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(start_frame)
            await proc.stdin.drain()
            outcome = await asyncio.wait_for(
                self._serve(proc, request, executor, state, allowed_tools, attempt), timeout=effective_timeout,
            )
        except asyncio.CancelledError:
            if proc is not None:
                await controller.stop(proc)
            raise
        except Exception:
            if proc is not None:
                await controller.stop(proc)
            return AgentResult(content="[subagent failed: worker unavailable]")
        finally:
            if proc is not None and proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
            controller.close()
        return AgentResult(content=outcome.content, turns=outcome.turns)

    async def _serve(self, proc: asyncio.subprocess.Process, request: WorkerRequest,
                     executor: ToolExecutor, state: _TrustedTurnState,
                     allowed_tools: frozenset[str], attempt: int = 0) -> WorkerOutcome:
        assert proc.stdout is not None and proc.stdin is not None
        frames = total_bytes = 0
        max_frames = request.max_iterations * (request.max_per_tool + 2)
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise WorkerProtocolError("worker exited without a result")
            frames += 1
            total_bytes += len(line)
            if frames > max_frames or total_bytes > 4 * 1_000_000:
                raise WorkerProtocolError("worker IPC budget exhausted")
            message = decode(line)
            if not self._authorized(request, attempt):
                raise WorkerProtocolError("worker capability is unavailable")
            kind = message.get("type")
            if kind == "result":
                if (message.get("request_id") != request.request_id
                        or message.get("run_id") != request.run_id
                        or message.get("delegation_id") != request.delegation_id):
                    raise WorkerProtocolError("worker result correlation mismatch")
                await proc.wait()
                if proc.returncode != 0:
                    raise WorkerProtocolError("worker reported a result then failed")
                return WorkerOutcome(str(message.get("content", "")), str(message.get("outcome", "failed")),
                                     int(message.get("turns", 0)))
            if kind == "error":
                raise WorkerProtocolError("worker returned a safe error")
            if kind not in {"model", "tool"}:
                raise WorkerProtocolError("worker requested an unsupported operation")
            reply = await self._handle(message, request, executor, state, allowed_tools, attempt)
            # A revoke can race with the parent-owned model/tool await above.
            # Do not deliver an in-flight privileged result to a worker whose
            # grant changed while the parent was computing it.
            if not self._authorized(request, attempt):
                raise WorkerProtocolError("worker capability is unavailable")
            proc.stdin.write(encode({"version": 1, "type": "reply",
                                     "request_id": message.get("request_id", ""), **reply}))
            await proc.stdin.drain()

    async def _handle(self, message: dict[str, Any], request: WorkerRequest,
                      executor: ToolExecutor, state: _TrustedTurnState,
                      allowed_tools: frozenset[str], attempt: int = 0) -> dict[str, Any]:
        try:
            if not self._authorized(request, attempt):
                raise WorkerProtocolError("worker capability is unavailable")
            if message["type"] == "model":
                state.model_calls += 1
                if state.model_calls > request.max_iterations + 1:
                    raise WorkerProtocolError("worker model-call budget exhausted")
                schemas = message.get("tools", [])
                if not isinstance(schemas, list):
                    raise WorkerProtocolError("model tools must be a list")
                if any(not isinstance(item, dict) or item.get("name") not in allowed_tools for item in schemas):
                    raise WorkerProtocolError("worker requested a tool outside its profile")
                # Treat worker-provided schemas only as an assertion of its
                # current view.  The authoritative descriptions and argument
                # schemas are reconstructed by the supervisor.
                supervised_schemas = [
                    {"name": tool.spec.name, "description": tool.spec.description,
                     "input_schema": tool.spec.parameters or {"type": "object", "properties": {}}}
                    for name, tool in scoped_specialist_tools(request.role, self._tools).items()
                    if name in allowed_tools
                    if tool.available()
                ]
                with bind_run_id(request.run_id), bind_delegation_id(request.delegation_id):
                    result: CompletionResult = await self._router.complete(
                        state.messages, system=(
                            system_prompt("", channel_hint=f"specialist:{request.role}")
                            + "\n\nYou are a HiveOS specialist. Follow the closed role and tool policy. "
                            f"Your role is {request.role}."
                        ),
                        tools=supervised_schemas,
                    )
                state.messages.append(Message(role=Role.ASSISTANT, content=result.text,
                                              tool_calls=result.tool_calls))
                state.pending = {call.id: call for call in result.tool_calls}
                return {"result": {"text": result.text, "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in result.tool_calls
                ]}}
            name = str(message.get("name", ""))
            if name not in allowed_tools:
                raise WorkerProtocolError("worker requested a tool outside its profile")
            args = message.get("args", {})
            if not isinstance(args, dict):
                raise WorkerProtocolError("worker tool arguments must be an object")
            call_id = next((call_id for call_id, call in state.pending.items()
                            if call.name == name), "")
            if not call_id:
                raise WorkerProtocolError("worker tool call was not authorized by the model")
            try:
                expected_args = json.loads(state.pending[call_id].arguments)
            except (TypeError, ValueError) as exc:
                raise WorkerProtocolError("model emitted invalid tool arguments") from exc
            if expected_args != args:
                raise WorkerProtocolError("worker tool arguments differ from model authorization")
            # Consume the model authorization before any dispatch outcome.
            # Replaying a denied/pending call must never create another approval
            # request or retry an effect without a new model decision.
            state.pending.pop(call_id, None)
            state.tool_calls[name] = state.tool_calls.get(name, 0) + 1
            if state.tool_calls[name] > request.max_per_tool:
                raise WorkerProtocolError("worker per-tool budget exhausted")
            with bind_run_id(request.run_id), bind_delegation_id(request.delegation_id):
                dispatch = await executor.execute(name, args, reason="requested by supervised worker",
                                                  run_id=request.run_id)
            if dispatch.status is DispatchStatus.OK and dispatch.result is not None:
                state.messages.append(Message(
                    role=Role.TOOL, content=ContentEnvelope.untrusted(
                        dispatch.result.content, source=f"tool:{name}",
                    ).render_for_prompt(), tool_call_id=call_id, name=name,
                ))
                return {"result": {"status": "ok", "content": dispatch.result.content}}
            if dispatch.status is DispatchStatus.PENDING:
                return {"result": {"status": "pending_approval", "approval_id": dispatch.approval_id or ""}}
            return {"result": {"status": "error", "error": dispatch.error or "worker tool refused"}}
        except Exception as exc:  # the child receives a class, never secret-bearing detail
            return {"error": type(exc).__name__}

    def _authorized(self, request: WorkerRequest, attempt: int) -> bool:
        if self._delegation_ledger is None:
            return True
        return bool(request.capability_id and self._delegation_ledger.authorize_attempt(
            request.delegation_id, capability_id=request.capability_id,
            role=request.role, attempt=attempt,
        ))
