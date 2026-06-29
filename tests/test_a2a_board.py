"""test_a2a_board.py — SPRINT_6 P-G BoardStore coverage (issue #75).

100% branch coverage on src/hive/agents/board.py.
"""
from __future__ import annotations

import time

import pytest

from hive.agents.a2a.events import (
    emit_call_completed,
    emit_call_failed,
    emit_call_started,
)
from hive.agents.board import BOARD_AGENTS, BoardCard, BoardStore
from hive.core.events import EventBus


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_board_agents_lists_exactly_five_named_subagents():
    assert BOARD_AGENTS == (
        "researcher", "coder", "reviewer",
        "memory-keeper", "security-reviewer",
    )


# ---------------------------------------------------------------------------
# Construction + initial state
# ---------------------------------------------------------------------------

def test_new_board_snapshot_returns_all_five_columns_empty():
    bus = EventBus()
    board = BoardStore(bus)
    snap = board.snapshot()
    assert set(snap.keys()) == set(BOARD_AGENTS)
    for cards in snap.values():
        assert cards == []


def test_board_on_different_bus_does_not_leak():
    """Two BoardStores on different EventBuses are fully isolated."""
    bus_a, bus_b = EventBus(), EventBus()
    a = BoardStore(bus_a)
    b = BoardStore(bus_b)
    emit_call_started(bus_a, method="x.run", request_id="r1",
                      agent_name="researcher", task="t")
    assert a.snapshot()["researcher"] and not b.snapshot()["researcher"]


# ---------------------------------------------------------------------------
# start → running
# ---------------------------------------------------------------------------

def test_started_event_adds_card_in_running_state():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_started(bus, method="researcher.run", request_id="r1",
                      agent_name="researcher", task="find x",
                      session_id="s1")
    [card] = board.snapshot()["researcher"]
    assert isinstance(card, BoardCard)
    assert card.request_id == "r1"
    assert card.agent_name == "researcher"
    assert card.method == "researcher.run"
    assert card.task == "find x"
    assert card.status == "running"
    assert card.finished_at is None
    assert card.result is None
    assert card.error is None
    assert card.tool_calls == 0
    assert card.session_id == "s1"
    assert card.started_at > 0


# ---------------------------------------------------------------------------
# completed → done
# ---------------------------------------------------------------------------

def test_completed_event_marks_card_done_with_result():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_started(bus, method="coder.run", request_id="r2",
                      agent_name="coder", task="t")
    emit_call_completed(bus, method="coder.run", request_id="r2",
                        agent_name="coder", result="all good")
    [card] = board.snapshot()["coder"]
    assert card.status == "done"
    assert card.result == "all good"
    assert card.finished_at is not None
    assert card.error is None


# ---------------------------------------------------------------------------
# failed → failed
# ---------------------------------------------------------------------------

def test_failed_event_marks_card_failed_with_error():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_started(bus, method="reviewer.run", request_id="r3",
                      agent_name="reviewer", task="t")
    emit_call_failed(bus, method="reviewer.run", request_id="r3",
                     agent_name="reviewer", error="boom")
    [card] = board.snapshot()["reviewer"]
    assert card.status == "failed"
    assert card.error == "boom"
    assert card.finished_at is not None
    assert card.result is None


# ---------------------------------------------------------------------------
# Completion without prior start (defensive)
# ---------------------------------------------------------------------------

def test_completed_without_started_is_ignored():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_completed(bus, method="x.run", request_id="ghost",
                        agent_name="coder", result="?")
    assert board.snapshot()["coder"] == []


def test_failed_without_started_is_ignored():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_failed(bus, method="x.run", request_id="ghost",
                     agent_name="coder", error="?")
    assert board.snapshot()["coder"] == []


# ---------------------------------------------------------------------------
# Multiple cards per agent
# ---------------------------------------------------------------------------

def test_multiple_concurrent_cards_appear_in_start_order():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_started(bus, method="coder.run", request_id="r1",
                      agent_name="coder", task="first")
    time.sleep(0.002)  # ensure distinct started_at
    emit_call_started(bus, method="coder.run", request_id="r2",
                      agent_name="coder", task="second")
    cards = board.snapshot()["coder"]
    assert [c.request_id for c in cards] == ["r1", "r2"]
    assert all(c.status == "running" for c in cards)


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------

def test_snapshot_prunes_cards_older_than_ttl(monkeypatch):
    bus = EventBus()
    board = BoardStore(bus, ttl_seconds=10)
    emit_call_started(bus, method="coder.run", request_id="r1",
                      agent_name="coder", task="old")
    [card] = board.snapshot()["coder"]
    # backdate
    card.started_at -= 100
    assert board.snapshot()["coder"] == []


def test_default_ttl_is_one_hour():
    bus = EventBus()
    board = BoardStore(bus)
    assert board.ttl_seconds == 3600


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------

def test_reset_clears_all_cards():
    bus = EventBus()
    board = BoardStore(bus)
    emit_call_started(bus, method="coder.run", request_id="r1",
                      agent_name="coder", task="t")
    board.reset()
    snap = board.snapshot()
    for cards in snap.values():
        assert cards == []


# ---------------------------------------------------------------------------
# REST endpoint: GET /agents/board (SPRINT_6 P-G, issue #75)
# ---------------------------------------------------------------------------

from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.runtime import HiveOS


_TOKEN = {"X-Hive-Token": "change_me"}


def _hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return _StubHive.build(cfg)


class _StubRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        from hive.llm.adapters.base import CompletionResult
        return CompletionResult(text="ok", model="t")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


_StubHive = type("_StubHive", (), {"build": staticmethod(
    lambda cfg: HiveOS.build(cfg, router=_StubRouter())
)})


def test_agents_board_requires_auth(tmp_path):
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.get("/agents/board")
        assert r.status_code == 401


def test_agents_board_returns_empty_columns_initially(tmp_path):
    with TestClient(create_app(_hive(tmp_path))) as c:
        r = c.get("/agents/board", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert set(body["columns"].keys()) == {
            "researcher", "coder", "reviewer",
            "memory-keeper", "security-reviewer",
        }
        for cards in body["columns"].values():
            assert cards == []


def test_agents_board_reflects_live_state(tmp_path):
    h = _hive(tmp_path)
    with TestClient(create_app(h)) as c:
        emit_call_started(h.events, method="coder.run", request_id="ep-1",
                          agent_name="coder", task="refactor module X",
                          session_id="sess-1")
        r = c.get("/agents/board", headers=_TOKEN)
        assert r.status_code == 200
        cards = r.json()["columns"]["coder"]
        assert len(cards) == 1
        c0 = cards[0]
        assert c0["request_id"] == "ep-1"
        assert c0["task"] == "refactor module X"
        assert c0["status"] == "running"
        assert c0["session_id"] == "sess-1"
        assert c0["finished_at"] is None


def test_ws_dashboard_forwards_a2a_started_event(tmp_path):
    """A connected /ws/dashboard client must receive a2a.call.started events."""
    h = _hive(tmp_path)
    with TestClient(create_app(h)) as c:
        with c.websocket_connect("/ws/dashboard") as ws:
            ws.send_text("change_me")  # token handshake
            # give the server a tick to subscribe
            import time as _t; _t.sleep(0.05)
            emit_call_started(h.events, method="reviewer.run",
                              request_id="ws-1", agent_name="reviewer",
                              task="t", session_id="s1")
            # collect messages until we see the a2a event (skip pings)
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "a2a.call.started":
                    assert msg["data"]["request_id"] == "ws-1"
                    assert msg["data"]["agent_name"] == "reviewer"
                    break
            else:
                pytest.fail("a2a.call.started not received within 20 frames")
