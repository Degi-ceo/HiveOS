"""Credential-free subprocess entry point for one local specialist turn."""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from hive.agents.base import AgentResult
from hive.agents.profiles import specialist_profile
from hive.agents.worker_protocol import WorkerProtocolError, decode, encode
from hive.core.types import ContentEnvelope, Message, ToolCall, ToolResult
from hive.llm.adapters.base import CompletionResult
from hive.tools.dispatch import DispatchStatus, ToolDispatch


async def _read_frame() -> dict[str, Any]:
    raw = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not raw:
        raise WorkerProtocolError("supervisor closed worker input")
    return decode(raw)


async def _write_frame(message: dict[str, Any]) -> None:
    frame = encode(message)
    await asyncio.to_thread(sys.stdout.buffer.write, frame)
    await asyncio.to_thread(sys.stdout.buffer.flush)


class _Rpc:
    async def call(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id", ""))
        await _write_frame({"version": 1, "type": kind, **payload})
        response = await _read_frame()
        if response.get("type") != "reply" or response.get("request_id") != request_id:
            raise WorkerProtocolError("unexpected supervisor reply")
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        result = response.get("result")
        if not isinstance(result, dict):
            raise WorkerProtocolError("supervisor reply has no result")
        return result


class _RouterProxy:
    def __init__(self, rpc: _Rpc) -> None:
        self._rpc = rpc

    async def complete(self, messages: list[Message], *, system: str | None = None,
                       tools: list[dict[str, Any]] | None = None, **_kwargs: Any) -> CompletionResult:
        request_id = str(__import__("uuid").uuid4())
        result = await self._rpc.call("model", {
            "request_id": request_id,
            "messages": [message.to_dict() for message in messages],
            "system": system or "",
            "tools": tools or [],
        })
        calls = [ToolCall(id=str(item["id"]), name=str(item["name"]),
                          arguments=str(item["arguments"]))
                 for item in result.get("tool_calls", [])]
        return CompletionResult(text=str(result.get("text", "")), model="supervisor",
                                tool_calls=calls)


class _ExecutorProxy:
    def __init__(self, rpc: _Rpc) -> None:
        self._rpc = rpc

    async def execute(self, name: str, args: dict[str, Any] | None = None, **_kwargs: Any) -> ToolDispatch:
        request_id = str(__import__("uuid").uuid4())
        result = await self._rpc.call("tool", {
            "request_id": request_id, "name": name, "args": dict(args or {}),
        })
        status = str(result.get("status", "error"))
        if status == DispatchStatus.OK.value:
            return ToolDispatch(DispatchStatus.OK, result=ToolResult.from_envelope(
                name, ContentEnvelope.untrusted(str(result.get("content", "")), source=f"tool:{name}"),
                success=True,
            ))
        if status == DispatchStatus.PENDING.value:
            return ToolDispatch(DispatchStatus.PENDING, approval_id=str(result.get("approval_id", "")))
        return ToolDispatch(DispatchStatus.ERROR, error=str(result.get("error", "worker tool refused")))


async def _run(start: dict[str, Any]) -> AgentResult:
    from hive.agents.orchestrator import ConversationOrchestrator
    from hive.tools.base import ToolSpec

    role = str(start.get("role", ""))
    profile = specialist_profile(role)
    rpc = _Rpc()
    schemas = start.get("tools", [])
    if not isinstance(schemas, list):
        raise WorkerProtocolError("start tools must be a list")
    tools = {}
    for schema in schemas:
        name = str(schema.get("name", ""))
        if name not in profile.allowed_tools:
            raise WorkerProtocolError("supervisor offered a tool outside the role profile")
        tools[name] = type("WorkerTool", (), {"spec": ToolSpec(
            name=name, description=str(schema.get("description", "")),
            parameters=dict(schema.get("input_schema", {})),
        ), "available": staticmethod(lambda: True)})()
    agent = ConversationOrchestrator(
        _RouterProxy(rpc), tools=tools, tool_executor=_ExecutorProxy(rpc),
        max_iterations=max(1, min(int(start.get("max_iterations", 30)), 30)),
        max_per_tool=max(1, min(int(start.get("max_per_tool", 50)), 50)),
        system_prompt_override=(
            "You are a HiveOS specialist running in an isolated worker. "
            f"Your role is {profile.name}. Follow only the role-scoped tools "
            "provided by your supervisor."
        ),
    )
    return await agent.ask(str(start.get("task", "")), session_id="worker")


async def main() -> int:
    try:
        start = await _read_frame()
        if start.get("type") != "start":
            raise WorkerProtocolError("worker expected start message")
        result = await _run(start)
        await _write_frame({
            "version": 1, "type": "result", "request_id": start.get("request_id", ""),
            "run_id": start.get("run_id", ""), "delegation_id": start.get("delegation_id", ""),
            "content": result.content, "outcome": result.outcome.value, "turns": result.turns,
        })
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # no raw traceback or environment reaches the supervisor
        await _write_frame({"version": 1, "type": "error", "error": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
