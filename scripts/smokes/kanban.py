"""
Smoke test the P-G Kanban endpoint + WS round-trip end-to-end.

Spins up the gateway with a fake `hive.ask`, fires 3 delegate_to_specialist
calls (one per agent) through the production tool path, verifies /agents/board
shows expected column shape and that /ws/dashboard forwards a2a.call.* events.

Usage:
    python scripts/smokes/kanban.py
Exit 0 on success, non-zero on any assertion failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

# Bootstrap env BEFORE importing hive.*
os.environ.setdefault("HIVE_TOKEN", "change_me")

from starlette.testclient import TestClient  # noqa: E402

from hive.agents.base import AgentResult, BaseAgent  # noqa: E402
from hive.agents.delegate import register_agent  # noqa: E402
from hive.core.config import HiveConfig  # noqa: E402
from hive.gateway.app import create_app  # noqa: E402
from hive.llm.adapters.base import CompletionResult  # noqa: E402
from hive.runtime import HiveOS  # noqa: E402


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="ok", model="smoke")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _hive(tmp):
    cfg = HiveConfig.from_env(root=tmp, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


def main() -> int:
    class _SmokeStub(BaseAgent):
        agent_id = "smoke-stub"

        async def run(self, input, context=None, **kw):
            return AgentResult(content=f"smoke-done:{input}")

    register_agent("smoke-stub", lambda: _SmokeStub())

    with tempfile.TemporaryDirectory() as tmp:
        h = _hive(tmp)
        with TestClient(create_app(h)) as c:
            tok = {"X-Hive-Token": "change_me"}
            delegate_tool = h.tools["delegate_to_specialist"]

            # 1. Empty board
            r = c.get("/agents/board", headers=tok)
            assert r.status_code == 200, f"GET /agents/board → {r.status_code}"
            cols = r.json()["columns"]
            assert set(cols.keys()) == {
                "researcher", "coder", "reviewer",
                "memory-keeper", "security-reviewer",
            }, f"unexpected columns: {set(cols.keys())}"
            for v in cols.values():
                assert v == [], f"expected empty column, got {v}"
            print("[ok] /agents/board returns 5 empty columns")

            # 2. Fire 3 delegations through the production tool path so A2A
            #    events are emitted on h.events (no direct emit_* — those
            #    would bypass the wiring under test).
            #    The smoke-stub agent is registered; the underlying
            #    delegate_via_envelope will pick it up via the shared
            #    _AGENT_REGISTRY.
            for agent_name in ("researcher", "coder", "reviewer"):
                # Use a *distinct* stub agent factory per column by re-registering
                # under the column name so BoardStore buckets them per-agent.
                register_agent(agent_name, lambda: _SmokeStub())
                result = asyncio.run(delegate_tool.execute(
                    agent=agent_name, task=f"work-{agent_name}",
                ))
                assert result.content.startswith("smoke-done:"), (
                    f"delegate_to_specialist({agent_name}) failed: {result.content}"
                )
                # Yield so request_id (UUID) is observable before assertion;
                # we don't need to read it back — board reflects started/completed.
                time.sleep(0.01)

            r = c.get("/agents/board", headers=tok)
            assert r.status_code == 200
            cols = r.json()["columns"]
            assert len(cols["researcher"]) == 1, cols["researcher"]
            assert cols["researcher"][0]["status"] == "done", cols["researcher"][0]
            assert cols["researcher"][0]["task"] == "work-researcher"
            assert len(cols["coder"]) == 1, cols["coder"]
            assert cols["coder"][0]["status"] == "done", cols["coder"][0]
            assert cols["coder"][0]["task"] == "work-coder"
            assert len(cols["reviewer"]) == 1, cols["reviewer"]
            assert cols["reviewer"][0]["status"] == "done", cols["reviewer"][0]
            assert cols["reviewer"][0]["task"] == "work-reviewer"
            for name in ("memory-keeper", "security-reviewer"):
                assert cols[name] == [], f"{name} should be empty"
            print("[ok] /agents/board reflects started/completed via prod delegate_to_specialist")

            # 3. WebSocket forwards a2a.call.started
            with c.websocket_connect("/ws/dashboard") as ws:
                ws.send_text("change_me")
                time.sleep(0.05)
                # Trigger a fresh delegation while the WS is open so we can
                # observe the started frame as it crosses the bus.
                register_agent("memory-keeper", lambda: _SmokeStub())
                result = asyncio.run(delegate_tool.execute(
                    agent="memory-keeper", task="watch",
                ))
                assert result.content.startswith("smoke-done:"), result.content
                seen_started = False
                for _ in range(20):
                    msg = ws.receive_json()
                    if (msg.get("type") == "a2a.call.started"
                            and msg["data"]["agent_name"] == "memory-keeper"
                            and msg["data"]["task"] == "watch"):
                        seen_started = True
                        break
                assert seen_started, "ws/dashboard did not forward a2a.call.started"
                print("[ok] /ws/dashboard forwards a2a.call.started")

        print("ALL OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
