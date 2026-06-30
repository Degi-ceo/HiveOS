"""
board.py — in-memory Kanban board snapshot (SPRINT_6 P-G, issue #75).

BoardStore subscribes to A2A lifecycle events (a2a.call.started/completed/failed)
and maintains a per-agent-card view consumable by GET /agents/board and the
Mission Control React Kanban component. Cards auto-prune after TTL so the
snapshot stays bounded for long-running daemons.

Threading invariant: BoardStore mutates ``self._cards`` only under
``self._lock`` (see the event handlers below). ``snapshot()`` returns the LIVE
BoardCard references held inside the store — consumers MUST treat them as
read-only and MUST NOT mutate fields. If you need your own copy, call
``dataclasses.replace(card)`` per card.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from hive.core.events import Event, EventBus, EventType

#: The five named sub-agents shown as Kanban columns.
BOARD_AGENTS: tuple[str, ...] = (
    "researcher", "coder", "reviewer",
    "memory-keeper", "security-reviewer",
)

Status = Literal["queued", "running", "done", "failed"]


@dataclass(slots=True)
class BoardCard:
    """One row on the Kanban board.

    Instances returned by BoardStore.snapshot() are live references held under
    the store's lock — DO NOT mutate fields. Card fields are only set under
    the store lock; cross-thread read access through .snapshot() is safe but
    read-only.
    """
    request_id: str
    method: str
    agent_name: str
    task: str
    status: Status
    started_at: float
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    tool_calls: int = 0
    session_id: str | None = None


class BoardStore:
    """Thread-safe snapshot of in-flight + recent A2A calls, keyed by agent.

    All mutations to ``self._cards`` happen under ``self._lock`` from the
    three event handlers. ``snapshot()`` borrows those references for the
    caller — see module-level docstring for the invariants.
    """

    def __init__(self, bus: EventBus, *, ttl_seconds: int = 3600) -> None:
        self._bus = bus
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._cards: dict[str, BoardCard] = {}  # request_id → card
        bus.subscribe(EventType.A2A_CALL_STARTED, self._on_started)
        bus.subscribe(EventType.A2A_CALL_COMPLETED, self._on_completed)
        bus.subscribe(EventType.A2A_CALL_FAILED, self._on_failed)

    # --- EventBus handlers -------------------------------------------------

    def _on_started(self, ev: Event) -> None:
        d = ev.data
        card = BoardCard(
            request_id=d["request_id"],
            method=d["method"],
            agent_name=d["agent_name"],
            task=d["task"],
            status="running",
            started_at=ev.timestamp,
            session_id=d.get("session_id"),
        )
        with self._lock:
            self._cards[card.request_id] = card

    def _on_completed(self, ev: Event) -> None:
        d = ev.data
        with self._lock:
            card = self._cards.get(d["request_id"])
            if card is None:
                return
            card.status = "done"
            card.finished_at = ev.timestamp
            card.result = d.get("result")

    def _on_failed(self, ev: Event) -> None:
        d = ev.data
        with self._lock:
            card = self._cards.get(d["request_id"])
            if card is None:
                return
            card.status = "failed"
            card.finished_at = ev.timestamp
            card.error = d.get("error")

    # --- Public API --------------------------------------------------------

    def snapshot(self) -> dict[str, list[BoardCard]]:
        """Return per-agent card lists. Prunes cards older than ttl_seconds.

        All 5 known agents always appear (empty list if no cards). Cards are
        ordered by started_at ascending (oldest first, matches Kanban "queue").
        """
        now = time.time()
        out: dict[str, list[BoardCard]] = {name: [] for name in BOARD_AGENTS}
        with self._lock:
            # prune
            for rid in list(self._cards):
                if now - self._cards[rid].started_at > self.ttl_seconds:
                    self._cards.pop(rid, None)
            # bucket
            for card in self._cards.values():
                out.setdefault(card.agent_name, []).append(card)
        for name in out:
            out[name].sort(key=lambda c: c.started_at)
        return out

    def reset(self) -> None:
        """Clear all cards. Test-only helper."""
        with self._lock:
            self._cards.clear()
