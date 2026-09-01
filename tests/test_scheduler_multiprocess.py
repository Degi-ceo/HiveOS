"""Process-level SQLite scheduler evidence (Windows ``spawn`` compatible)."""
from __future__ import annotations

import multiprocessing
from pathlib import Path

from hive.autonomy.commitments import CommitmentBook
from hive.autonomy.cron import CronScheduler
from hive.autonomy.tasks import TaskBoard


def _run_due_scheduler(
    kind: str, database: str, now: float, start: object, ready: object, results: object,
) -> None:
    """Open fresh connections in a spawned process and attempt one scheduler tick."""
    board = TaskBoard(database, clock=lambda: now)
    scheduler = (
        CronScheduler(database, board, clock=lambda: now)
        if kind == "cron" else CommitmentBook(database, board, clock=lambda: now)
    )
    ready.put("ready")
    try:
        if not start.wait(15):
            results.put(("error", "start signal timed out"))
            return
        results.put(("ok", scheduler.due_and_enqueue(now)))
    except Exception as exc:  # pragma: no cover - evidence is asserted by the parent
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        scheduler.close()
        board.close()


def _run_two_processes(kind: str, database: Path, now: float) -> list[tuple[str, object]]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    workers = [
        context.Process(
            target=_run_due_scheduler,
            args=(kind, str(database), now, start, ready, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        assert [ready.get(timeout=15) for _ in workers] == ["ready", "ready"]
        start.set()
        output = [results.get(timeout=15) for _ in workers]
    finally:
        start.set()
        for worker in workers:
            worker.join(timeout=15)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
    assert all(worker.exitcode == 0 for worker in workers)
    return output


def test_two_processes_enqueue_one_cron_occurrence(tmp_path):
    database = tmp_path / "cron-processes.sqlite"
    board = TaskBoard(database, clock=lambda: 0.0)
    scheduler = CronScheduler(database, board, clock=lambda: 0.0)
    job_id = scheduler.add("@hourly", "tool", {"tool": "health"})
    due_at = scheduler.get(job_id).next_run + 1
    scheduler.close()
    board.close()

    outcome = _run_two_processes("cron", database, due_at)

    assert sorted(outcome) == [("ok", 0), ("ok", 1)]
    verified_board = TaskBoard(database)
    verified_scheduler = CronScheduler(database, verified_board)
    try:
        assert verified_board.total_count() == 1
        job = verified_scheduler.get(job_id)
        assert job is not None and job.last_run == due_at and job.next_run > due_at
    finally:
        verified_scheduler.close()
        verified_board.close()


def test_two_processes_enqueue_one_commitment_occurrence(tmp_path):
    database = tmp_path / "commitment-processes.sqlite"
    board = TaskBoard(database, clock=lambda: 100.0)
    commitments = CommitmentBook(database, board, clock=lambda: 100.0)
    commitment_id = commitments.add("check health", cadence_seconds=60, payload={"tool": "health"})
    commitments.close()
    board.close()

    outcome = _run_two_processes("commitment", database, 100.0)

    assert sorted(outcome) == [("ok", 0), ("ok", 1)]
    verified_board = TaskBoard(database)
    verified_commitments = CommitmentBook(database, verified_board)
    try:
        assert verified_board.total_count() == 1
        commitment = verified_commitments.get(commitment_id)
        assert commitment is not None and commitment.last_fulfilled == 100.0
    finally:
        verified_commitments.close()
        verified_board.close()
