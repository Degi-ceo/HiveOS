"""
Smoke test the P-G Kanban endpoint + WS round-trip end-to-end.

Spins up the gateway with a fake `hive.ask`, fires 3 delegate_to_specialist
calls (one per agent), verifies /agents/board shows expected column shape and
that /ws/dashboard forwards a2a.call.* events.

Usage:
    python scripts/smokes/kanban.py
Exit 0 on success, non-zero on any assertion failure.
"""
from __future__ import annotations

import os
import sys
import time

# Bootstrap env BEFORE importing hive.*
os.environ.setdefault("HIVE_TOKEN", "change_me")

from starlette.testclient import TestClient  # noqa: E402

from hive.core.config import HiveConfig  # noqa: E402
from hive.gateway.app import create_app  # noqa: E402
from hive.llm.adapters.base import CompletionResult  # noqa: E402
from hive.runtime import HiveOS  # noqa: E402


class _Router:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="ok", model="smoke")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _hive(tmp):
    cfg = HiveConfig.from_env(root=tmp, load_dotenv=False)
    return HiveOS.build(cfg, router=_Router())


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = _hive(tmp)
        with TestClient(create_app(h)) as c:
            tok = {"X-Hive-Token": "change_me"}

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

            # 2. Publish events via the bus and re-read
            from hive.agents.a2a.events import (
                emit_call_completed,
                emit_call_failed,
                emit_call_started,
            )
            emit_call_started(h.events, method="researcher.run",
                              request_id="smoke-r1", agent_name="researcher",
                              task="find docs", session_id="sess-r1")
            emit_call_completed(h.events, method="researcher.run",
                                request_id="smoke-r1", agent_name="researcher",
                                result="done")
            emit_call_started(h.events, method="coder.run",
                              request_id="smoke-c1", agent_name="coder",
                              task="refactor", session_id="sess-c1")
            emit_call_failed(h.events, method="coder.run",
                             request_id="smoke-c1", agent_name="coder",
                             error="boom")

            r = c.get("/agents/board", headers=tok)
            assert r.status_code == 200
            cols = r.json()["columns"]
            assert len(cols["researcher"]) == 1
            assert cols["researcher"][0]["status"] == "done"
            assert cols["researcher"][0]["session_id"] == "sess-r1"
            assert len(cols["coder"]) == 1
            assert cols["coder"][0]["status"] == "failed"
            assert cols["coder"][0]["error"] == "boom"
            for name in ("reviewer", "memory-keeper", "security-reviewer"):
                assert cols[name] == [], f"{name} should be empty"
            print("[ok] /agents/board reflects started/completed/failed")

            # 3. WebSocket forwards a2a.call.started
            with c.websocket_connect("/ws/dashboard") as ws:
                ws.send_text("change_me")
                time.sleep(0.05)
                emit_call_started(h.events, method="reviewer.run",
                                  request_id="smoke-rev1", agent_name="reviewer",
                                  task="review PR", session_id="sess-rev1")
                seen_started = False
                for _ in range(20):
                    msg = ws.receive_json()
                    if (msg.get("type") == "a2a.call.started"
                            and msg["data"]["request_id"] == "smoke-rev1"):
                        seen_started = True
                        break
                assert seen_started, "ws/dashboard did not forward a2a.call.started"
                print("[ok] /ws/dashboard forwards a2a.call.started")

        print("ALL OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
