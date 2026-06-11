"""
test_m10_self_improve.py — M10-c: self-improvement depth.

Tests:
  - TaskBoard.recent_failures() query
  - HiveOS.self_improve_from_symptom() is callable and returns a list
  - Heartbeat tick returns self_improved key
  - REVIEW/MANUAL edits are enqueued as self_improve tasks
"""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="[]", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# TaskBoard.recent_failures
# ---------------------------------------------------------------------------

def test_recent_failures_empty(tmp_path):
    hive = _make_hive(tmp_path)
    assert hive.task_board.recent_failures() == []


def test_recent_failures_returns_failed_tasks(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "timeout")

    failures = hive.task_board.recent_failures(limit=10)
    assert len(failures) == 1
    assert failures[0].state == "failed"
    assert failures[0].last_error == "timeout"


def test_recent_failures_only_failed_state(tmp_path):
    hive = _make_hive(tmp_path)
    # Enqueue 3: one done, one failed, one pending
    tid1 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid1)
    hive.task_board.complete(tid1)

    tid2 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid2)
    hive.task_board.fail(tid2, "err")

    hive.task_board.enqueue("tool", {}, source="test")  # pending

    failures = hive.task_board.recent_failures()
    assert len(failures) == 1
    assert failures[0].state == "failed"


def test_recent_failures_limit(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(5):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    assert len(hive.task_board.recent_failures(limit=3)) == 3


def test_recent_failures_newest_first(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(3):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    failures = hive.task_board.recent_failures()
    ids = [f.id for f in failures]
    assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# HiveOS.self_improve_from_symptom
# ---------------------------------------------------------------------------

def test_self_improve_from_symptom_returns_list(tmp_path):
    hive = _make_hive(tmp_path)
    outcomes = asyncio.run(hive.self_improve_from_symptom("repeated timeout errors"))
    assert isinstance(outcomes, list)


def test_self_improve_from_symptom_no_crash_on_empty_response(tmp_path):
    hive = _make_hive(tmp_path)
    # _ScriptRouter returns text="[]" so diagnoser returns no edits — safe no-op
    outcomes = asyncio.run(hive.self_improve_from_symptom("test symptom"))
    assert outcomes == []


# ---------------------------------------------------------------------------
# Heartbeat tick — self_improved key present
# ---------------------------------------------------------------------------

def test_tick_returns_self_improved_key(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert "self_improved" in result
    assert isinstance(result["self_improved"], int)


def test_tick_self_improve_not_triggered_below_threshold(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    # Only 2 failures — below the ≥3 threshold; self_improved should stay 0
    for _ in range(2):
        tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, "err")

    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert result["self_improved"] == 0
