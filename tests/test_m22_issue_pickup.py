"""Durable, single-flight GitHub issue pickup."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from hive.autonomy.tasks import TaskBoard


def test_pickup_is_stable_across_restart_and_pruning(tmp_path):
    db = tmp_path / "state.db"
    now = [100.0]
    board = TaskBoard(db, clock=lambda: now[0])
    task_id = board.enqueue_issue_work("Degi-ceo", "HiveOS", 140)
    assert task_id is not None
    task = board.get(task_id)
    assert task.kind == "issue_work"
    assert task.payload == {"owner": "Degi-ceo", "repo": "HiveOS", "number": 140}
    assert task.max_attempts == 1
    assert board.owns_issue_pickup(task_id, "Degi-ceo", "HiveOS", 140)
    assert not board.owns_issue_pickup(task_id, "Degi-ceo", "HiveOS", 141)
    assert board.claim(task_id)
    assert board.complete(task_id)
    now[0] += 86401
    assert board.purge_done(max_age_seconds=86400) == 1
    restarted = TaskBoard(db, clock=lambda: now[0])
    assert restarted.enqueue_issue_work("degi-ceo", "hiveos", 140) is None
    assert restarted.enqueue_issue_work("Degi-ceo", "HiveOS", 141) is not None


def test_pickup_cap_counts_pending_and_awaiting_approval(tmp_path):
    board = TaskBoard(tmp_path / "state.db")
    first = board.enqueue_issue_work("owner", "repo", 1)
    assert first is not None
    assert board.enqueue_issue_work("owner", "repo", 2) is None
    assert board.claim(first)
    assert board.await_approval(first, "approval-1")
    assert board.enqueue_issue_work("owner", "repo", 2) is None
    assert board.resolve_approval("approval-1", approved=True) == 1
    assert board.enqueue_issue_work("owner", "repo", 2) is not None


def test_pickup_is_atomic_across_connections(tmp_path):
    db = tmp_path / "state.db"
    boards = [TaskBoard(db), TaskBoard(db)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda board: board.enqueue_issue_work("owner", "repo", 1), boards,
        ))
    assert sum(result is not None for result in results) == 1
    assert len(boards[0].due()) == 1


def test_restart_does_not_reclaim_a_live_issue_lease(tmp_path):
    db = tmp_path / "state.db"
    now = [1000.0]
    first = TaskBoard(db, clock=lambda: now[0])
    task_id = first.enqueue_issue_work("owner", "repo", 140)
    assert task_id is not None
    assert first.claim(task_id)
    restarted = TaskBoard(db, clock=lambda: now[0])
    assert restarted.requeue_running() == 0
    assert restarted.get(task_id).state == "running"
    now[0] += 301
    assert restarted.requeue_running() == 0  # exhausted one-shot work becomes dead
    assert restarted.get(task_id).state == "dead"


def test_stale_recovery_rechecks_lease_at_atomic_transition(tmp_path, monkeypatch):
    now = [1000.0]
    board = TaskBoard(tmp_path / "state.db", clock=lambda: now[0])
    task_id = board.enqueue_issue_work("owner", "repo", 140)
    assert task_id is not None
    assert board.claim(task_id)
    now[0] = 1400.0
    original = board.stall_or_dead

    def renew_between_select_and_update(*args, **kwargs):
        assert board.touch_running(task_id, expected_attempt=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(board, "stall_or_dead", renew_between_select_and_update)
    assert board.recover_stalled(stall_after_seconds=300) == 0
    assert board.get(task_id).state == "running"
    assert board.enqueue_issue_work("owner", "repo", 141) is None


@pytest.mark.parametrize("owner,repo,number,cap", [
    ("../owner", "repo", 1, 1),
    ("owner", ".", 1, 1),
    ("owner", "repo", 0, 1),
    ("owner", "repo", True, 1),
    ("owner", "repo", 1, 0),
    ("owner", "repo", 1, 5),
])
def test_invalid_pickup_identity_is_rejected(tmp_path, owner, repo, number, cap):
    board = TaskBoard(tmp_path / "state.db")
    with pytest.raises(ValueError):
        board.enqueue_issue_work(owner, repo, number, max_inflight=cap)
