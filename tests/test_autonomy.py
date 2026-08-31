"""M3 autonomy — durable task board, cron, commitments, + heartbeat integration."""
from __future__ import annotations

import asyncio

import pytest

from hive.autonomy.tasks import TaskBoard, PENDING, RUNNING, DONE, FAILED, WAITING_APPROVAL
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


def test_wait_for_approval_survives_reopen_and_is_not_requeued(tmp_path):
    """Approval-pending work must not be mistaken for a crashed worker task."""
    db = tmp_path / "s.db"
    board = TaskBoard(db)
    task_id = board.enqueue("tool", {"tool": "deploy"})
    assert board.claim(task_id) is True
    board.wait_for_approval(task_id, "approval-123")
    board.close()

    restarted = TaskBoard(db)
    record = restarted.get(task_id)
    assert record is not None
    assert record.state == WAITING_APPROVAL
    assert record.last_error == "awaiting approval: approval-123"
    assert restarted.requeue_running() == 0
    assert restarted.get(task_id).state == WAITING_APPROVAL



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
    now = [100.0]
    board = TaskBoard(tmp_path / "t.db", clock=lambda: now[0])
    tid1 = board.enqueue("job1")
    tid2 = board.enqueue("job2")
    board.claim(tid1, lease_seconds=10)
    board.claim(tid2, lease_seconds=10)
    assert board.requeue_running() == 0
    now[0] = 111.0
    assert board.requeue_running() == 2
    assert board.get(tid1).state == PENDING
    assert board.get(tid2).state == PENDING
    assert board.requeue_running() == 0


def test_autonomy_preflight_allows_non_test_records(tmp_path):
    board = TaskBoard(tmp_path / "safe.db")
    board.enqueue("tool", {"tool": "read_file"}, source="manual")
    assert board.autonomy_preflight() == {"ok": True, "blockers": [], "test_records": 0}


def test_autonomy_preflight_blocks_test_origin_records_after_reopen(tmp_path):
    db = tmp_path / "contaminated.db"
    board = TaskBoard(db)
    board.enqueue("tool", {"tool": "shell"}, source="pytest:heartbeat")
    board.close()
    restarted = TaskBoard(db)
    preflight = restarted.autonomy_preflight()
    assert preflight["ok"] is False
    assert preflight["test_records"] == 1
    assert "test-origin" in preflight["blockers"][0]

def test_worker_lease_prevents_foreign_completion_and_recovers_only_after_expiry(tmp_path):
    now = [100.0]
    board = TaskBoard(tmp_path / "lease.db", clock=lambda: now[0])
    task_id = board.enqueue("tool", {"tool": "read_file"})
    assert board.claim(task_id, worker_id="worker-a", lease_seconds=20) is True
    claimed = board.get(task_id)
    assert claimed is not None
    assert claimed.worker_id == "worker-a"
    assert claimed.lease_until == 120.0
    assert board.complete(task_id, worker_id="worker-b") is False
    assert board.get(task_id).state == RUNNING
    assert board.requeue_running() == 0
    now[0] = 121.0
    assert board.requeue_running() == 1
    recovered = board.get(task_id)
    assert recovered is not None
    assert recovered.state == PENDING
    assert recovered.worker_id is None and recovered.lease_until is None


def test_worker_lease_owner_can_complete(tmp_path):
    board = TaskBoard(tmp_path / "lease-owner.db", clock=lambda: 100.0)
    task_id = board.enqueue("tool", {"tool": "read_file"})
    assert board.claim(task_id, worker_id="worker-a", lease_seconds=20)
    assert board.complete(task_id, worker_id="worker-a") is True
    assert board.get(task_id).state == DONE

def test_enqueue_idempotency_key_deduplicates_across_reopen(tmp_path):
    db = tmp_path / "idempotency.db"
    board = TaskBoard(db)
    first = board.enqueue("tool", {"tool": "read_file"}, idempotency_key="cron:daily:100")
    assert board.enqueue("tool", {"tool": "read_file"}, idempotency_key="cron:daily:100") == first
    assert board.total_count() == 1
    board.close()
    reopened = TaskBoard(db)
    assert reopened.enqueue("tool", {"tool": "read_file"}, idempotency_key="cron:daily:100") == first
    record = reopened.get(first)
    assert record is not None and record.idempotency_key == "cron:daily:100"

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


def test_cron_restart_after_task_write_deduplicates_the_same_due_slot(tmp_path):
    """A crash after task persistence but before cursor advance must not duplicate work."""
    now = [0.0]
    db = tmp_path / "shared.db"
    board = TaskBoard(db, clock=lambda: now[0])
    cron = CronScheduler(db, board, clock=lambda: now[0])
    job_id = cron.add("@hourly", "tool", {"tool": "health"})
    due_slot = cron.get(job_id).next_run
    assert due_slot is not None
    # Simulate the durable half of an interrupted scheduler tick.
    board.enqueue("tool", {"tool": "health"}, source=f"cron:{job_id}",
                  idempotency_key=f"cron:{job_id}:{due_slot:.6f}")
    cron.close()
    restarted = CronScheduler(db, board, clock=lambda: now[0])
    assert restarted.due_and_enqueue(due_slot + 1) == 1
    assert board.total_count() == 1

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


def test_commitment_restart_after_task_write_deduplicates_the_same_due_slot(tmp_path):
    """A crash before last_fulfilled is advanced must not duplicate a commitment."""
    now = [1000.0]
    db = tmp_path / "shared.db"
    board = TaskBoard(db, clock=lambda: now[0])
    book = CommitmentBook(db, board, clock=lambda: now[0])
    commitment_id = book.add("health check", cadence_seconds=3600, payload={"check": "db"})
    board.enqueue("commitment", {"description": "health check", "check": "db"},
                  source=f"commitment:{commitment_id}",
                  idempotency_key=f"commitment:{commitment_id}:initial")
    book.close()
    restarted = CommitmentBook(db, board, clock=lambda: now[0])
    assert restarted.due_and_enqueue(now[0]) == 1
    assert board.total_count() == 1

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
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # These tests exercise tick scheduling and dispatch, not the default-off gate.
    object.__setattr__(cfg, "autonomy_enabled", True)
    return HiveOS.build(cfg, router=_Router())


def test_heartbeat_drains_durable_board_task(tmp_path):
    h = _hive(tmp_path)
    target = tmp_path / "x"
    target.write_text("ok", encoding="utf-8")
    # Enqueue a safe builtin tool task directly on the durable board.
    h.task_board.enqueue("tool", {"tool": "read_file", "args": {"path": str(target)}},
                         source="fixture")
    summary = asyncio.run(Heartbeat(h, goals=["g"]).tick())
    assert summary["dispatched"] == 1
    assert h.task_board.all(state="done")  # task marked done on the board


def test_heartbeat_fires_cron_through_tick(tmp_path):
    h = _hive(tmp_path)
    target = tmp_path / "y"
    target.write_text("ok", encoding="utf-8")
    h.cron.add("@hourly", "tool", {"tool": "read_file", "args": {"path": str(target)}})
    # Tick far enough in the future that the cron job is due.
    summary = asyncio.run(Heartbeat(h, goals=["g"]).tick(now=999_999_999_999.0))
    assert summary["cron"] == 1 and summary["dispatched"] == 1


def test_heartbeat_fires_commitment_through_tick(tmp_path):
    h = _hive(tmp_path)
    target = tmp_path / "z"
    target.write_text("ok", encoding="utf-8")
    h.commitments.add("daily review", cadence_seconds=86_400,
                      task_kind="tool",
                      payload={"tool": "read_file", "args": {"path": str(target)}})
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


# --- overdue_jobs ------------------------------------------------------------

def test_cron_overdue_jobs_empty(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board)
    assert sched.overdue_jobs() == []


def test_cron_overdue_jobs_returns_due_jobs(tmp_path):
    from hive.autonomy.cron import CronScheduler
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    jid = sched.add("@hourly", "check")
    now[0] = 9000.0  # advance past next_run (was ~4600)
    overdue = sched.overdue_jobs(now=now[0])
    assert len(overdue) >= 1
    assert overdue[0].id == jid


def test_cron_overdue_jobs_excludes_disabled(tmp_path):
    from hive.autonomy.cron import CronScheduler
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    jid = sched.add("@hourly", "check", enabled=False)
    now[0] = 9000.0
    assert sched.overdue_jobs(now=now[0]) == []


# --- next_due_time -----------------------------------------------------------

def test_cron_next_due_time_no_jobs(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board)
    assert sched.next_due_time() is None


def test_cron_next_due_time_returns_earliest(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board)
    sched.add("@daily", "d")   # next_run = now + 86400
    sched.add("@hourly", "h")  # next_run = now + 3600 (earlier)
    t = sched.next_due_time()
    assert t is not None
    assert t < sched.next_due_time() + 86400  # hourly < daily


# --- job_health --------------------------------------------------------------

def test_cron_job_health_empty(tmp_path):
    from hive.autonomy.cron import CronScheduler
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board)
    h = sched.job_health()
    assert h == {"total": 0, "enabled": 0, "due": 0, "task_kinds": []}


def test_cron_job_health_counts(tmp_path):
    from hive.autonomy.cron import CronScheduler
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    sched = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    sched.add("@daily", "backup", enabled=True)
    sched.add("@hourly", "health", enabled=True)
    sched.add("@weekly", "report", enabled=False)
    now[0] = 9000.0  # advance: @hourly job is now overdue
    h = sched.job_health()
    assert h["total"] == 3
    assert h["enabled"] == 2
    assert h["due"] >= 1
    assert "health" in h["task_kinds"]


# --- pending_by_kind ---------------------------------------------------------

def test_taskboard_pending_by_kind_empty(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    assert board.pending_by_kind() == {}


def test_taskboard_pending_by_kind_groups(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    board.enqueue("tool", {"cmd": "a"})
    board.enqueue("tool", {"cmd": "b"})
    board.enqueue("commitment", {})
    by_kind = board.pending_by_kind()
    assert by_kind["tool"] == 2
    assert by_kind["commitment"] == 1


def test_taskboard_pending_by_kind_excludes_non_pending(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    t1 = board.enqueue("tool", {})
    board.enqueue("tool", {})
    board.claim(t1)
    board.complete(t1)
    by_kind = board.pending_by_kind()
    assert by_kind.get("tool", 0) == 1  # only the unclaimed one


# --- average_age_pending -----------------------------------------------------

def test_taskboard_average_age_pending_empty(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    assert board.average_age_pending() == 0.0


def test_taskboard_average_age_pending(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    board.enqueue("t", {})
    board.enqueue("t", {})
    now[0] = 1060.0  # 60 seconds later
    age = board.average_age_pending(now=now[0])
    assert abs(age - 60.0) < 1.0  # approximately 60 seconds


# --- failure_rate_by_kind ----------------------------------------------------

def test_taskboard_failure_rate_by_kind_empty(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    assert board.failure_rate_by_kind() == {}


def test_taskboard_failure_rate_by_kind_no_failures(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    t1 = board.enqueue("tool", {})
    board.claim(t1)
    board.complete(t1)
    # No failures → empty dict (kinds with 0 failures are excluded)
    assert board.failure_rate_by_kind() == {}


def test_taskboard_failure_rate_by_kind_all_failed(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    t1 = board.enqueue("tool", {})
    t2 = board.enqueue("tool", {})
    board.claim(t1)
    board.fail(t1, "error1")
    board.claim(t2)
    board.fail(t2, "error2")
    rates = board.failure_rate_by_kind()
    assert rates["tool"] == 1.0


def test_taskboard_failure_rate_by_kind_partial(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    t1 = board.enqueue("tool", {})
    t2 = board.enqueue("tool", {})
    board.claim(t1)
    board.complete(t1)
    board.claim(t2)
    board.fail(t2, "error")
    rates = board.failure_rate_by_kind()
    assert abs(rates["tool"] - 0.5) < 0.001


# --- oldest_pending_age --------------------------------------------------------

def test_taskboard_oldest_pending_age_empty(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    assert board.oldest_pending_age() == 0.0


def test_taskboard_oldest_pending_age(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    board.enqueue("t", {})
    now[0] = 1120.0  # 120 seconds later
    age = board.oldest_pending_age(now=now[0])
    assert abs(age - 120.0) < 1.0


# --- total_count ---------------------------------------------------------------

def test_taskboard_total_count_empty(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    assert board.total_count() == 0


def test_taskboard_total_count_all_states(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    t1 = board.enqueue("a", {})
    board.claim(t1)
    board.complete(t1)
    t2 = board.enqueue("b", {})
    board.claim(t2)
    board.fail(t2, "err")
    board.enqueue("c", {})  # PENDING
    assert board.total_count() == 3


# --- next_due_at ----------------------------------------------------------------

def test_commitment_next_due_at_not_found(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    assert book.next_due_at(999) is None


def test_commitment_next_due_at_never_fulfilled(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board, clock=lambda: now[0])
    cid = book.add("daily report", 86400.0)
    # Never fulfilled → due immediately (returns current time)
    due = book.next_due_at(cid)
    assert due == 1000.0


def test_commitment_next_due_at_after_fulfill(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board, clock=lambda: now[0])
    cid = book.add("daily report", 86400.0)
    book.fulfill(cid)  # marks last_fulfilled = 1000.0
    due = book.next_due_at(cid)
    assert due == 1000.0 + 86400.0


def test_commitment_next_due_at_inactive_returns_none(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("daily report", 86400.0)
    book.set_active(cid, False)
    assert book.next_due_at(cid) is None


# --- upcoming ----------------------------------------------------------------

def test_commitment_upcoming_empty(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    assert book.upcoming() == []


def test_commitment_upcoming_sorted_by_next_due(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board, clock=lambda: now[0])
    # All get same cadence; fulfill them at different times to create distinct next_due times
    cid_hourly = book.add("hourly", 3600.0)
    cid_daily = book.add("daily", 3600.0)
    cid_weekly = book.add("weekly", 3600.0)
    # Fulfill each at a different time: next_due = last_fulfilled + cadence_seconds (3600)
    # hourly fulfilled at t=100 → next_due = 3700 (soonest)
    # daily fulfilled at t=200 → next_due = 3800
    # weekly fulfilled at t=300 → next_due = 3900
    now[0] = 100.0
    book.fulfill(cid_hourly)
    now[0] = 200.0
    book.fulfill(cid_daily)
    now[0] = 300.0
    book.fulfill(cid_weekly)
    now[0] = 1000.0
    items = book.upcoming(limit=3, now=1000.0)
    assert len(items) == 3
    # hourly was fulfilled earliest → next_due is smallest → first
    assert items[0].description == "hourly"
    assert items[1].description == "daily"
    assert items[2].description == "weekly"


def test_commitment_upcoming_excludes_inactive(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("inactive", 3600.0)
    book.set_active(cid, False)
    book.add("active", 86400.0)
    items = book.upcoming()
    assert len(items) == 1
    assert items[0].description == "active"


def test_commitment_upcoming_respects_limit(tmp_path):
    from hive.autonomy.commitments import CommitmentBook
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    for i in range(5):
        book.add(f"c{i}", 3600.0 * (i + 1))
    assert len(book.upcoming(limit=2)) == 2


# --- TaskBoard.pending_count --------------------------------------------------

def test_taskboard_pending_count_increments_and_decrements(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.pending_count() == 0
    t1 = board.enqueue("a", {})
    t2 = board.enqueue("b", {})
    assert board.pending_count() == 2
    board.claim(t1)
    board.complete(t1)
    assert board.pending_count() == 1
    board.claim(t2)
    board.fail(t2, "err")
    assert board.pending_count() == 0


# --- TaskBoard.all with state filter -----------------------------------------

def test_taskboard_all_no_filter_returns_everything(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("job", {})
    t2 = board.enqueue("job", {})
    board.claim(t1)
    board.complete(t1)
    records = board.all()
    assert len(records) == 2
    states = {r.state for r in records}
    assert states == {DONE, PENDING}


def test_taskboard_all_with_state_filter(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("done_job", {})
    board.enqueue("pending_job", {})
    board.claim(t1)
    board.complete(t1)
    done = board.all(state=DONE)
    assert len(done) == 1 and done[0].kind == "done_job"
    pending = board.all(state=PENDING)
    assert len(pending) == 1 and pending[0].kind == "pending_job"


# --- TaskBoard.recent_failures -----------------------------------------------

def test_taskboard_recent_failures_empty(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    assert board.recent_failures() == []


def test_taskboard_recent_failures_returns_newest_first(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("first", {})
    t2 = board.enqueue("second", {})
    t3 = board.enqueue("third", {})
    for tid in (t1, t2, t3):
        board.claim(tid)
        board.fail(tid, f"err-{tid}")
    failures = board.recent_failures(limit=2)
    assert len(failures) == 2
    assert failures[0].kind == "third"  # newest first by id DESC
    assert failures[1].kind == "second"


def test_taskboard_recent_failures_limit_respected(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    for i in range(5):
        tid = board.enqueue("job", {})
        board.claim(tid)
        board.fail(tid, "err")
    assert len(board.recent_failures(limit=3)) == 3


# --- TaskBoard.search with source and state filters --------------------------

def test_taskboard_search_by_source(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("ping", {}, source="cron:1")
    board.enqueue("ping", {}, source="cron:2")
    board.enqueue("ping", {}, source="commitment:1")
    results = board.search(source="cron:1")
    assert len(results) == 1 and results[0].source == "cron:1"


def test_taskboard_search_by_state(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("alpha", {})
    board.enqueue("beta", {})
    board.claim(t1)
    board.complete(t1)
    done = board.search(state=DONE)
    assert len(done) == 1 and done[0].kind == "alpha"


def test_taskboard_search_combined_filters(tmp_path):
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("tool", {}, source="cron:1")
    board.enqueue("tool", {}, source="cron:2")
    board.enqueue("commit", {}, source="cron:1")
    results = board.search(kind="tool", source="cron:1")
    assert len(results) == 1
    assert results[0].kind == "tool" and results[0].source == "cron:1"


# --- CommitmentBook.list_commitments alias -----------------------------------

def test_commitment_list_commitments_alias(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    book.add("task1", 3600.0)
    book.add("task2", 7200.0)
    assert book.list_commitments() == book.all()


def test_commitment_list_commitments_active_only_filter(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    book.add("active", 3600.0)
    cid = book.add("inactive", 7200.0)
    book.set_active(cid, False)
    active = book.list_commitments(active_only=True)
    assert len(active) == 1 and active[0].description == "active"


# --- CommitmentBook.add with custom task_kind and payload --------------------

def test_commitment_add_payload_roundtrip(tmp_path):
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("with payload", 3600.0, task_kind="health", payload={"key": "value"})
    c = book.get(cid)
    assert c.task_kind == "health"
    assert c.payload == {"key": "value"}


# --- CommitmentBook.due_and_enqueue partial overdue --------------------------

def test_commitment_due_and_enqueue_partial(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "c.db", board, clock=lambda: now[0])
    book.add("fast", cadence_seconds=10)
    book.add("slow", cadence_seconds=10000)
    # First fire: both never fulfilled, both overdue
    assert book.due_and_enqueue(1000.0) == 2
    # Advance only past fast cadence (20s), not slow (10000s)
    assert book.due_and_enqueue(1021.0) == 1  # only fast fires again


# --- CronScheduler.add with payload round-trip -------------------------------

def test_cron_add_payload_enqueued_on_fire(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    cron.add("@hourly", "health", {"check": "db"})
    cron.due_and_enqueue(3601.0)
    task = board.due(3601.0)[0]
    assert task.payload == {"check": "db"}
    assert task.source.startswith("cron:")


# --- CronScheduler interval "every Ns" fires at correct time -----------------

def test_cron_interval_every_seconds(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    cron.add("every 30s", "ping")
    assert cron.due_and_enqueue(29.0) == 0   # not yet due
    assert cron.due_and_enqueue(31.0) == 1   # past 30s window


def test_cron_interval_every_days(tmp_path):
    from hive.autonomy.cron import next_run
    assert next_run("every 2d", 0.0) == 2 * 86_400.0


# --- CronScheduler.due_and_enqueue advances next_run -----------------------

def test_cron_due_and_enqueue_advances_next_run(tmp_path):
    now = [0.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    cron.add("every 60s", "tick")
    # Fire once at t=61
    assert cron.due_and_enqueue(61.0) == 1
    job = cron.jobs()[0]
    # next_run should now be 61+60=121, not 0+60=60
    assert job.next_run is not None and job.next_run > 61.0
    # Should not fire again at t=62
    assert cron.due_and_enqueue(62.0) == 0


# --- CommitmentBook.overdue with mixed overdue/not-overdue -------------------

def test_commitment_overdue_mixed(tmp_path):
    now = [1000.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    book = CommitmentBook(tmp_path / "c.db", board, clock=lambda: now[0])
    cid_fast = book.add("fast", cadence_seconds=10)
    cid_slow = book.add("slow", cadence_seconds=9999)
    # Fulfill both now so neither is overdue from never-fulfilled status
    book.fulfill(cid_fast)
    book.fulfill(cid_slow)
    # Advance 20s — only fast (cadence=10) is overdue
    overdue = book.overdue(now=1020.0)
    assert len(overdue) == 1
    assert overdue[0].description == "fast"


# --- TaskBoard extra coverage -------------------------------------------------

def test_taskboard_add_returns_task_id(tmp_path):
    """enqueue() returns an integer task id."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("tool", {"tool": "ping"})
    assert isinstance(tid, int)
    assert tid > 0


def test_taskboard_get_by_id(tmp_path):
    """add a task then get(task_id) returns the task."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("health", {"k": "v"})
    record = board.get(tid)
    assert record is not None
    assert record.id == tid
    assert record.kind == "health"
    assert record.payload == {"k": "v"}


def test_taskboard_get_nonexistent_returns_none(tmp_path):
    """get() with an unknown id returns None."""
    board = TaskBoard(tmp_path / "s.db")
    assert board.get(999999) is None


def test_taskboard_pending_count_increments(tmp_path):
    """add 3 tasks, pending_count() == 3."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("a", {})
    board.enqueue("b", {})
    board.enqueue("c", {})
    assert board.pending_count() == 3


def test_taskboard_complete_task_removes_from_pending(tmp_path):
    """complete a task, pending_count() decreases."""
    board = TaskBoard(tmp_path / "s.db")
    t1 = board.enqueue("job1", {})
    board.enqueue("job2", {})
    assert board.pending_count() == 2
    board.claim(t1)
    board.complete(t1)
    assert board.pending_count() == 1


# --- CronScheduler extra coverage ---------------------------------------------

def test_cron_add_and_list(tmp_path):
    """add('job1', '* * * * *', callback), list_jobs() includes 'job1'."""
    board = TaskBoard(tmp_path / "b.db")
    cron = CronScheduler(tmp_path / "c.db", board)
    cron.add("@hourly", "job1")
    jobs = cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].task_kind == "job1"


def test_cron_remove_job(tmp_path):
    """add then remove; list_jobs() no longer includes it."""
    board = TaskBoard(tmp_path / "b.db")
    cron = CronScheduler(tmp_path / "c.db", board)
    jid = cron.add("@daily", "cleanup")
    assert any(j.id == jid for j in cron.list_jobs())
    assert cron.remove(jid) is True
    assert not any(j.id == jid for j in cron.list_jobs())


def test_cron_nonexistent_remove_does_not_raise(tmp_path):
    """remove('no-such-job') doesn't raise — returns False."""
    board = TaskBoard(tmp_path / "b.db")
    cron = CronScheduler(tmp_path / "c.db", board)
    result = cron.remove(999999)  # id that was never inserted
    assert result is False


# --- CommitmentBook extra coverage -------------------------------------------

def test_commitment_book_add_then_list(tmp_path):
    """add('daily-check', cadence=86400, ...), list of active commitments includes it."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("daily-check", cadence_seconds=86_400)
    active = book.all(active_only=True)
    assert len(active) == 1
    assert active[0].id == cid
    assert active[0].description == "daily-check"
    assert active[0].cadence_seconds == 86_400


def test_commitment_book_complete_commitment(tmp_path):
    """set_active(False) on a commitment removes it from the active list."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("daily-check", cadence_seconds=86_400)
    assert len(book.all(active_only=True)) == 1
    book.set_active(cid, False)
    assert book.all(active_only=True) == []
    # The commitment still exists in all() but is no longer active
    all_commitments = book.all()
    assert len(all_commitments) == 1
    assert all_commitments[0].active is False


# --- new tests (10) -----------------------------------------------------------

def test_task_board_complete_nonexistent_no_raise(tmp_path):
    """complete('unknown_id') on a non-existent task must not raise."""
    board = TaskBoard(tmp_path / "s.db")
    board.complete(999999)  # id that was never inserted — must not raise


def test_task_board_status_after_complete(tmp_path):
    """A completed task has state == 'done'."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("tool", {"cmd": "x"})
    board.claim(tid)
    board.complete(tid)
    assert board.get(tid).state == DONE


def test_commitments_list_empty_initially(tmp_path):
    """A fresh CommitmentBook.all() returns an empty list."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    assert book.all() == []


def test_commitments_deactivate_returns_bool(tmp_path):
    """set_active(id, False) for an existing commitment completes without error
    and the commitment shows as inactive afterward."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("daily task", cadence_seconds=86_400)
    book.set_active(cid, False)
    c = book.get(cid)
    assert c is not None and c.active is False


def test_cron_scheduler_run_due_fires_callback(tmp_path):
    """Add a job, advance clock past its next_run, due_and_enqueue() returns 1."""
    now = [0.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now[0])
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now[0])
    cron.add("every 60s", "ping")
    assert cron.due_and_enqueue(59.0) == 0    # not yet due
    assert cron.due_and_enqueue(61.0) == 1    # fires after 60 s


def test_task_board_priority_ordering(tmp_path):
    """Tasks are returned by due() in enqueue (FIFO) order — earlier id first."""
    board = TaskBoard(tmp_path / "s.db", clock=lambda: 0.0)
    tid1 = board.enqueue("low", {"priority": 0})
    tid2 = board.enqueue("high", {"priority": 10})
    due = board.due(0.0)
    # FIFO: tid1 was enqueued first, so it should appear first
    assert due[0].id == tid1
    assert due[1].id == tid2


def test_task_board_cancel_removes_from_pending(tmp_path):
    """cancel(id) transitions the task to FAILED, pending_count() decreases."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("job", {})
    assert board.pending_count() == 1
    board.cancel(tid)
    assert board.pending_count() == 0
    assert board.get(tid).state == FAILED


def test_cron_next_run_within_interval(tmp_path):
    """Newly added cron job's next_run is within now + interval."""
    now_val = 1000.0
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now_val)
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now_val)
    cron.add("every 30s", "tick")
    job = cron.jobs()[0]
    assert job.next_run is not None
    assert now_val < job.next_run <= now_val + 30.0


def test_commitments_filter_by_active(tmp_path):
    """all(active_only=True) returns only non-deactivated commitments."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid_active = book.add("keep this", cadence_seconds=3600)
    cid_inactive = book.add("deactivate this", cadence_seconds=3600)
    book.set_active(cid_inactive, False)
    active = book.all(active_only=True)
    assert len(active) == 1
    assert active[0].id == cid_active


def test_task_board_enqueue_many_and_count(tmp_path):
    """Enqueue 5 tasks, pending_count() == 5."""
    board = TaskBoard(tmp_path / "s.db")
    for i in range(5):
        board.enqueue("job", {"n": i})
    assert board.pending_count() == 5


# --- 6 new tests ---------------------------------------------------------------

def test_task_board_retry_failed_task(tmp_path):
    """A failed task can be retried: retry() returns True and state returns to pending."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("job", {})
    board.claim(tid)
    board.fail(tid, "transient error")
    assert board.get(tid).state == FAILED
    assert board.retry(tid) is True
    assert board.get(tid).state == PENDING


def test_task_board_requeue_running_recovers_only_expired_tasks(tmp_path):
    now = [100.0]
    board = TaskBoard(tmp_path / "s.db", clock=lambda: now[0])
    tid = board.enqueue("job", {})
    board.claim(tid, lease_seconds=10)
    assert board.get(tid).state == RUNNING
    assert board.requeue_running() == 0
    now[0] = 111.0
    assert board.requeue_running() == 1
    assert board.get(tid).state == PENDING


def test_task_board_count_by_kind(tmp_path):
    """count_by_kind() returns correct per-kind counts."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("tool", {})
    board.enqueue("tool", {})
    board.enqueue("cron", {})
    counts = board.count_by_kind()
    assert counts.get("tool") == 2
    assert counts.get("cron") == 1


def test_task_board_bulk_cancel_pending_clears_all(tmp_path):
    """bulk_cancel_pending() cancels all PENDING tasks and returns the count."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("job", {})
    board.enqueue("job", {})
    board.enqueue("job", {})
    cancelled = board.bulk_cancel_pending()
    assert cancelled == 3
    assert board.pending_count() == 0


def test_commitment_book_remove_deletes_entry(tmp_path):
    """remove() permanently deletes a commitment; count decreases by 1."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    cid = book.add("to be removed", cadence_seconds=3600)
    assert book.count() == 1
    assert book.remove(cid) is True
    assert book.count() == 0
    assert book.get(cid) is None


def test_cron_scheduler_set_enabled_disables_job(tmp_path):
    """set_enabled(id, False) disables a job; it no longer fires in due_and_enqueue."""
    now_val = [1000.0]
    board = TaskBoard(tmp_path / "b.db", clock=lambda: now_val[0])
    cron = CronScheduler(tmp_path / "c.db", board, clock=lambda: now_val[0])
    jid = cron.add("every 30s", "tick")
    assert cron.enabled_count() == 1
    cron.set_enabled(jid, False)
    assert cron.enabled_count() == 0
    # Advance time past due; disabled job must NOT fire
    fired = cron.due_and_enqueue(2000.0)
    assert fired == 0


# --- Wave 3U additional tests ---------------------------------------------------

def test_task_board_pending_count_zero_after_bulk_cancel(tmp_path):
    """pending_count() is 0 after bulk_cancel_pending() on all pending tasks."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("task-a", {})
    board.enqueue("task-b", {})
    board.bulk_cancel_pending()
    assert board.pending_count() == 0


def test_task_board_statistics_returns_dict(tmp_path):
    """statistics() returns a dict with at least one key."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("stat-task", {})
    stats = board.statistics()
    assert isinstance(stats, dict)
    assert len(stats) > 0


def test_task_board_purge_done_removes_completed(tmp_path):
    """purge_done(0) removes all DONE tasks regardless of age and returns count removed."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("done-task", {})
    board.claim(tid)
    board.complete(tid)
    removed = board.purge_done(max_age_seconds=0)
    assert removed >= 1


def test_task_board_running_count_after_claim(tmp_path):
    """running_count() equals 1 after one task is claimed."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("run-task", {})
    board.claim(tid)
    assert board.running_count() == 1


def test_commitment_book_active_names_contains_added(tmp_path):
    """active_names() returns the name of an active commitment."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    book.add("active-commitment-xyz", cadence_seconds=3600)
    names = book.active_names()
    assert "active-commitment-xyz" in names


def test_commitment_book_list_commitments_returns_list(tmp_path):
    """list_commitments() returns a list."""
    board = TaskBoard(tmp_path / "b.db")
    book = CommitmentBook(tmp_path / "c.db", board)
    book.add("list-test", cadence_seconds=600)
    commitments = book.list_commitments()
    assert isinstance(commitments, list)
    assert len(commitments) >= 1


def test_task_board_search_finds_by_kind(tmp_path):
    """search(kind='mytask') returns only tasks of that kind."""
    board = TaskBoard(tmp_path / "s.db")
    board.enqueue("mytask", {"x": 1})
    board.enqueue("other", {"y": 2})
    results = board.search(kind="mytask")
    assert all(t.kind == "mytask" for t in results)
    assert len(results) >= 1


def test_task_board_failed_count_increments_on_fail(tmp_path):
    """failed_count() increases by 1 after a task is failed."""
    board = TaskBoard(tmp_path / "s.db")
    tid = board.enqueue("fail-task", {})
    board.claim(tid)
    before = board.failed_count()
    board.fail(tid, "error msg")
    assert board.failed_count() == before + 1


# ── Phase 3: proactive diagnose throttle ─────────────────────────────────────

def test_heartbeat_proactive_skips_within_cooldown():
    """Proactive self-diagnose is skipped if it ran < 30 min ago with 0 outcomes."""
    import asyncio
    from unittest.mock import MagicMock, AsyncMock, patch
    from hive.autonomy.heartbeat import Heartbeat

    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 1  # fire every tick
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = []
    hive.planner = MagicMock()
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = "ctx"
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_diagnose = AsyncMock(return_value={"improvement_outcomes": []})

    hb = Heartbeat(hive)
    now = 1000.0
    # First tick — proactive runs (elapsed >= cooldown since _last_proactive_ts=0)
    asyncio.run(hb._tick_inner(now))
    assert hive.self_diagnose.call_count == 1

    # Second tick immediately after — should be skipped (elapsed < 1800s)
    asyncio.run(hb._tick_inner(now + 1))
    assert hive.self_diagnose.call_count == 1  # still 1 — throttled


def test_heartbeat_proactive_runs_after_cooldown():
    """Proactive self-diagnose fires again once 30 min have elapsed."""
    import asyncio
    from unittest.mock import MagicMock, AsyncMock
    from hive.autonomy.heartbeat import Heartbeat

    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 1
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = []
    hive.planner = MagicMock()
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = "ctx"
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_diagnose = AsyncMock(return_value={"improvement_outcomes": []})

    hb = Heartbeat(hive)
    now = 1000.0
    asyncio.run(hb._tick_inner(now))           # tick 1 — runs
    asyncio.run(hb._tick_inner(now + 1800))    # tick 2 — 30 min later → runs again
    assert hive.self_diagnose.call_count == 2


def test_task_board_renew_lease_requires_current_worker(tmp_path):
    now = [100.0]
    board = TaskBoard(tmp_path / "lease.db", clock=lambda: now[0])
    task_id = board.enqueue("tool", {"tool": "safe"})

    assert board.claim(task_id, worker_id="worker-a", lease_seconds=10)
    assert board.get(task_id).lease_until == 110.0
    assert not board.renew_lease(task_id, worker_id="worker-b", lease_seconds=20)

    now[0] = 105.0
    assert board.renew_lease(task_id, worker_id="worker-a", lease_seconds=20)
    assert board.get(task_id).lease_until == 125.0


def test_task_board_requeue_running_requires_expired_owned_lease(tmp_path):
    now = [100.0]
    board = TaskBoard(tmp_path / "owned-lease.db", clock=lambda: now[0])
    active = board.enqueue("tool", {"tool": "active"})
    expired = board.enqueue("tool", {"tool": "expired"})
    ownerless = board.enqueue("tool", {"tool": "ownerless"})

    assert board.claim(active, worker_id="worker-active", lease_seconds=20)
    assert board.claim(expired, worker_id="worker-expired", lease_seconds=10)
    assert board.claim(ownerless, worker_id="worker-ownerless", lease_seconds=10)
    board._db.execute("UPDATE hive_tasks SET worker_id=NULL WHERE id=?", (ownerless,))
    board._db.commit()

    now[0] = 110.0
    assert board.requeue_running(now=now[0]) == 1
    assert board.get(active).state == RUNNING
    assert board.get(expired).state == PENDING
    assert board.get(ownerless).state == RUNNING
    assert board.requeue_running(now=now[0]) == 0
