"""M3 autonomy — durable task board, cron, commitments, + heartbeat integration."""
from __future__ import annotations

import asyncio

import pytest

from hive.autonomy.tasks import TaskBoard, PENDING, RUNNING, DONE, FAILED
from hive.autonomy.cron import CronScheduler, next_run, HAS_CRONITER
from hive.autonomy.commitments import CommitmentBook


# --- TaskBoard -----------------------------------------------------------------

def test_enqueue_due_claim_complete(tmp_path):
    b = TaskBoard(tmp_path / "s.db", clock=lambda: 100.0)
    tid = b.enqueue("tool", {"tool": "shell", "args": {"cmd": "ls"}})
    due = b.due(100.0)
    assert len(due) == 1 and due[0].id == tid
    assert due[0].payload["tool"] == "shell"          # payload round-trips
    assert b.claim(tid) is True
    assert b.claim(tid) is False                       # already claimed
    assert b.get(tid).state == RUNNING
    b.complete(tid)
    assert b.get(tid).state == DONE


def test_fail_records_error(tmp_path):
    b = TaskBoard(tmp_path / "s.db")
    tid = b.enqueue("tool", {"tool": "x"})
    b.claim(tid)
    b.fail(tid, "boom")
    rec = b.get(tid)
    assert rec.state == FAILED and rec.last_error == "boom"


def test_scheduled_future_not_due(tmp_path):
    b = TaskBoard(tmp_path / "s.db", clock=lambda: 100.0)
    b.enqueue("tool", {"tool": "x"}, scheduled_for=200.0)
    assert b.due(100.0) == []
    assert len(b.due(250.0)) == 1


def test_board_durable_across_reopen(tmp_path):
    """#au-2: a queued task survives a restart (new connection to same DB)."""
    db = tmp_path / "s.db"
    b1 = TaskBoard(db, clock=lambda: 100.0)
    b1.enqueue("tool", {"tool": "persist_me"})
    b1.close()
    b2 = TaskBoard(db, clock=lambda: 100.0)   # "restart"
    due = b2.due(100.0)
    assert len(due) == 1 and due[0].payload["tool"] == "persist_me"


# --- cancel / retry (items 38-39) ---------------------------------------------

def test_task_board_cancel(tmp_path):
    board = TaskBoard(tmp_path / "t.db")
    tid = board.enqueue("test_job")
    assert board.cancel(tid) is True
    task = board.get(tid)
    assert task.state == FAILED
    assert board.cancel(tid) is False


def test_task_board_retry(tmp_path):
    board = TaskBoard(tmp_path / "t.db")
    tid = board.enqueue("test_job")
    board.claim(tid)
    board.fail(tid, "oops")
    assert board.retry(tid) is True
    task = board.get(tid)
    assert task.state == PENDING
    assert board.retry(tid) is False


def test_task_board_requeue_running(tmp_path):
    board = TaskBoard(tmp_path / "t.db")
    tid1 = board.enqueue("job1")
    tid2 = board.enqueue("job2")
    board.claim(tid1)
    board.claim(tid2)
    count = board.requeue_running()
    assert count == 2
    assert board.get(tid1).state == PENDING
    assert board.get(tid2).state == PENDING
    # Calling again with no running tasks returns 0
    assert board.requeue_running() == 0


# --- cron next_run -------------------------------------------------------------

def test_next_run_aliases_and_intervals():
    assert next_run("@hourly", 0.0) == 3600.0
    assert next_run("@daily", 0.0) == 86_400.0
    assert next_run("@weekly", 0.0) == 604_800.0
    assert next_run("every 30s", 0.0) == 30.0
    assert next_run("every 5m", 0.0) == 300.0
    assert next_run("every 2h", 0.0) == 7200.0


def test_next_run_unparseable_without_croniter():
    if HAS_CRONITER:
        pytest.skip("croniter installed — arbitrary cron expressions parse")
    assert next_run("*/5 * * * *", 0.0) is None


# --- CronScheduler -------------------------------------------------------------

def test_cron_fires_when_due_and_advances(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "s.db", board, clock=lambda: now[0])
    cron.add("@hourly", "tool", {"tool": "health"})
    assert cron.due_and_enqueue(0.0) == 0          # next_run is +3600, not due yet
    fired = cron.due_and_enqueue(3601.0)           # now past next_run
    assert fired == 1
    assert len(board.due(3601.0)) == 1
    job = cron.jobs()[0]
    assert job.next_run > 3601.0                    # advanced


def test_cron_disabled_does_not_fire(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "s.db", board, clock=lambda: now[0])
    jid = cron.add("@hourly", "tool", {"tool": "x"})
    cron.set_enabled(jid, False)
    assert cron.due_and_enqueue(99999.0) == 0


def test_cron_remove_deletes_job(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    cron = CronScheduler(tmp_path / "s.db", board)
    jid = cron.add("@hourly", "health")
    assert len(cron.jobs()) == 1
    assert cron.remove(jid) is True
    assert cron.jobs() == []
    assert cron.remove(jid) is False  # already removed


def test_cron_list_jobs_alias(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    cron = CronScheduler(tmp_path / "s.db", board)
    cron.add("@daily", "ping")
    assert cron.list_jobs() == cron.jobs()


# --- CommitmentBook ------------------------------------------------------------

def test_commitment_fires_when_never_fulfilled_then_waits(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "s.db", board, clock=lambda: now[0])
    book.add("daily health check", cadence_seconds=86_400, payload={"k": "v"})
    assert book.due_and_enqueue(1000.0) == 1       # never fulfilled -> fires
    assert book.due_and_enqueue(1000.0) == 0       # just fulfilled -> waits
    assert book.due_and_enqueue(1000.0 + 86_401) == 1   # cadence elapsed -> fires
    # task payload carries the description for the executor
    task = board.due(1000.0 + 86_402)[-1]
    assert task.payload["description"] == "daily health check"


def test_commitment_inactive_does_not_fire(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "s.db", board, clock=lambda: now[0])
    cid = book.add("x", cadence_seconds=10)
    book.set_active(cid, False)
    assert book.due_and_enqueue(99999.0) == 0


def test_commitment_remove(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    book = CommitmentBook(tmp_path / "s.db", board)
    cid = book.add("daily check", cadence_seconds=3600)
    assert book.remove(cid) is True
    assert book.all() == []
    assert book.remove(cid) is False  # already removed


def test_commitment_reschedule(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    book = CommitmentBook(tmp_path / "s.db", board)
    cid = book.add("weekly report", cadence_seconds=604800)
    assert book.reschedule(cid, 3600) is True
    [c] = book.all()
    assert c.cadence_seconds == 3600
    assert book.reschedule(9999, 100) is False  # unknown id


def test_commitment_get_by_id(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    book = CommitmentBook(tmp_path / "s.db", board)
    cid = book.add("daily check", cadence_seconds=3600, task_kind="health")
    c = book.get(cid)
    assert c is not None
    assert c.description == "daily check"
    assert c.cadence_seconds == 3600
    assert book.get(9999) is None  # unknown


def test_commitment_fulfill_resets_overdue_clock(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "s.db", board, clock=lambda: now[0])
    cid = book.add("daily", cadence_seconds=3600)
    assert book.due_and_enqueue(0.0) == 1  # first time fires
    now[0] = 100.0
    assert book.fulfill(cid) is True
    assert book.due_and_enqueue(100.0) == 0  # just fulfilled, cadence not elapsed


def test_taskboard_statistics(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("ping", {})
    board.enqueue("pong", {})
    tid = board.enqueue("fail", {})
    board.claim(tid)
    board.fail(tid, "boom")
    stats = board.statistics()
    assert stats["total"] == 3
    assert "pending" in stats["by_state"]
    assert "failed" in stats["by_state"]


def test_taskboard_search_by_kind(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("alpha", {})
    board.enqueue("beta", {})
    board.enqueue("alpha", {"x": 1})
    results = board.search(kind="alpha")
    assert len(results) == 2
    assert all(r.kind == "alpha" for r in results)


def test_taskboard_purge_done(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    t1 = board.enqueue("job", {})
    t2 = board.enqueue("job", {})
    board.claim(t1); board.complete(t1)
    board.claim(t2); board.complete(t2)
    # Advance time by 2 days
    now[0] += 86400 * 2
    purged = board.purge_done(max_age_seconds=86400)
    assert purged == 2
    assert board.all() == []


def test_taskboard_purge_done_keeps_recent(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    t1 = board.enqueue("job", {})
    board.claim(t1); board.complete(t1)
    # Don't advance time — task is still fresh
    purged = board.purge_done(max_age_seconds=3600)
    assert purged == 0


def test_taskboard_retry_all_failed(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("job", {})
    t2 = board.enqueue("job", {})
    board.claim(t1); board.fail(t1, "err1")
    board.claim(t2); board.fail(t2, "err2")
    retried = board.retry_all_failed()
    assert retried == 2
    assert all(t.state == "pending" for t in board.all())


# --- heartbeat integration -----------------------------------------------------

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.autonomy.heartbeat import Heartbeat


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="ok", model="fake")

    async def aclose(self):
        pass


def _hive(tmp_path) -> HiveOS:
    return HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_Router())


def test_heartbeat_drains_durable_board_task(tmp_path):
    h = _hive(tmp_path)
    # Enqueue a safe builtin tool task directly on the durable board.
    h.task_board.enqueue("tool", {"tool": "read_file", "args": {"path": str(tmp_path / "x")}},
                         source="test")
    summary = asyncio.run(Heartbeat(h, goals=["g"]).tick())
    assert summary["dispatched"] == 1
    assert h.task_board.all(state="done")  # task marked done on the board


def test_heartbeat_fires_cron_through_tick(tmp_path):
    h = _hive(tmp_path)
    h.cron.add("@hourly", "tool", {"tool": "read_file", "args": {"path": str(tmp_path / "y")}})
    # Tick far enough in the future that the cron job is due.
    summary = asyncio.run(Heartbeat(h, goals=["g"]).tick(now=999_999_999_999.0))
    assert summary["cron"] == 1 and summary["dispatched"] == 1


def test_heartbeat_fires_commitment_through_tick(tmp_path):
    h = _hive(tmp_path)
    h.commitments.add("daily review", cadence_seconds=86_400,
                      task_kind="tool",
                      payload={"tool": "read_file", "args": {"path": str(tmp_path / "z")}})
    summary = asyncio.run(Heartbeat(h, goals=["g"]).tick(now=1_000_000.0))
    assert summary["commitments"] == 1 and summary["dispatched"] == 1


# --- CommitmentBook.overdue() + count() ----------------------------------------

def test_commitment_overdue_returns_never_fulfilled(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "s.db", board, clock=lambda: now[0])
    book.add("overdue task", cadence_seconds=3600)
    overdue = book.overdue(now=1000.0)
    assert len(overdue) == 1
    assert overdue[0].description == "overdue task"


def test_commitment_overdue_excludes_recently_fulfilled(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "s.db", board, clock=lambda: now[0])
    cid = book.add("hourly ping", cadence_seconds=3600)
    book.fulfill(cid)          # marks last_fulfilled=0.0
    # 30 minutes later — not yet overdue
    overdue = book.overdue(now=1800.0)
    assert overdue == []
    # 2 hours later — overdue again
    overdue2 = book.overdue(now=7201.0)
    assert len(overdue2) == 1


def test_commitment_overdue_excludes_inactive(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    book = CommitmentBook(tmp_path / "s.db", board)
    cid = book.add("inactive task", cadence_seconds=0)
    book.set_active(cid, False)
    assert book.overdue() == []


def test_commitment_count(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    book = CommitmentBook(tmp_path / "s.db", board)
    assert book.count() == 0
    book.add("a", cadence_seconds=3600)
    cid = book.add("b", cadence_seconds=7200)
    book.set_active(cid, False)
    assert book.count() == 2
    assert book.count(active_only=True) == 1


# --- CronScheduler.get() -------------------------------------------------------

def test_cron_get_returns_job(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    cron = CronScheduler(tmp_path / "c.db", board)
    jid = cron.add("@hourly", "health_check")
    job = cron.get(jid)
    assert job is not None
    assert job.id == jid and job.schedule == "@hourly" and job.task_kind == "health_check"


def test_cron_get_unknown_returns_none(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    cron = CronScheduler(tmp_path / "c.db", board)
    assert cron.get(9999) is None


# --- TaskBoard.failed_count() and oldest_pending() ----------------------------

def test_taskboard_failed_count(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.failed_count() == 0
    t1 = board.enqueue("job", {})
    t2 = board.enqueue("job", {})
    board.claim(t1)
    board.fail(t1, "error msg")
    assert board.failed_count() == 1
    board.claim(t2)
    board.fail(t2, "also failed")
    assert board.failed_count() == 2


def test_taskboard_oldest_pending(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.oldest_pending() is None   # empty
    board.enqueue("first", {"n": 1})
    board.enqueue("second", {"n": 2})
    oldest = board.oldest_pending()
    assert oldest is not None
    assert oldest.payload["n"] == 1   # first enqueued


def test_taskboard_oldest_pending_excludes_non_pending(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("done_job", {})
    board.enqueue("pending_job", {})
    board.claim(t1)
    board.complete(t1)
    oldest = board.oldest_pending()
    assert oldest is not None
    assert oldest.kind == "pending_job"


def test_taskboard_count_by_kind(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.count_by_kind() == {}
    board.enqueue("tool", {})
    board.enqueue("tool", {})
    board.enqueue("commitment", {})
    board.enqueue("self_improve", {})
    counts = board.count_by_kind()
    assert counts["tool"] == 2
    assert counts["commitment"] == 1
    assert counts["self_improve"] == 1


def test_taskboard_bulk_cancel_pending_all(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("tool", {})
    board.enqueue("tool", {})
    board.enqueue("commit", {})
    cancelled = board.bulk_cancel_pending()
    assert cancelled == 3
    assert board.pending_count() == 0


def test_taskboard_bulk_cancel_pending_by_kind(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("tool", {})
    board.enqueue("tool", {})
    board.enqueue("commit", {})
    cancelled = board.bulk_cancel_pending(kind="tool")
    assert cancelled == 2
    assert board.pending_count() == 1   # commit still pending


def test_taskboard_bulk_cancel_pending_skips_non_pending(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("tool", {})
    board.claim(tid)
    board.complete(tid)
    cancelled = board.bulk_cancel_pending()
    assert cancelled == 0   # DONE task not affected


def test_taskboard_bulk_purge_failed(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    tid = board.enqueue("tool", {})
    board.claim(tid)
    board.fail(tid, "oops")
    # not old enough yet
    assert board.bulk_purge_failed(max_age_seconds=500) == 0
    now[0] = 2000.0
    purged = board.bulk_purge_failed(max_age_seconds=500)
    assert purged == 1
    assert board.failed_count() == 0


def test_taskboard_running_count(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.running_count() == 0
    t1 = board.enqueue("a", {})
    t2 = board.enqueue("b", {})
    board.claim(t1)
    assert board.running_count() == 1
    board.claim(t2)
    assert board.running_count() == 2
    board.complete(t1)
    assert board.running_count() == 1


def test_commitment_active_names(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    assert book.active_names() == []
    c1 = book.add("daily standup", 86400)
    c2 = book.add("weekly review", 604800)
    names = book.active_names()
    assert names == ["daily standup", "weekly review"]
    book.set_active(c1, False)
    assert book.active_names() == ["weekly review"]


def test_commitment_active_names_excludes_removed(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("task", 3600)
    book.remove(cid)
    assert book.active_names() == []


def test_cron_due_count_none_due(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    now = [1000.0]
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    sched.add("@daily", "check", enabled=True)
    assert sched.due_count(now=999.0) == 0  # next_run not yet reached


def test_cron_due_count_one_due(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    now = [1000.0]
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    sched.add("@daily", "check", enabled=True)
    assert sched.due_count(now=1000.0 + 86_401) == 1  # past next_run


def test_cron_due_count_excludes_disabled(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    now = [1000.0]
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    jid = sched.add("@hourly", "check", enabled=True)
    sched.set_enabled(jid, False)
    assert sched.due_count(now=1000.0 + 7201) == 0  # disabled


def test_taskboard_last_failed_none_when_empty(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.last_failed() is None


def test_taskboard_last_failed_returns_most_recent(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("a", {})
    t2 = board.enqueue("b", {})
    board.claim(t1)
    board.fail(t1, "first error")
    board.claim(t2)
    board.fail(t2, "second error")
    last = board.last_failed()
    assert last is not None
    assert last.last_error == "second error"  # most recently failed by id


def test_cron_enabled_count(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board)
    assert sched.enabled_count() == 0
    j1 = sched.add("@daily", "a", enabled=True)
    j2 = sched.add("@daily", "b", enabled=True)
    sched.add("@daily", "c", enabled=False)
    assert sched.enabled_count() == 2
    sched.set_enabled(j1, False)
    assert sched.enabled_count() == 1
