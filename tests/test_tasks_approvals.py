from hive.autonomy.tasks import AWAITING_APPROVAL, DONE, FAILED, TaskBoard


def test_task_waiting_for_approval_is_resolved_by_approval_id():
    board = TaskBoard(":memory:")
    task_id = board.enqueue("tool", {"tool": "deploy"})
    assert board.claim(task_id)
    assert board.await_approval(task_id, "approval-1")
    assert board.get(task_id).state == AWAITING_APPROVAL
    assert board.resolve_approval("approval-1", approved=True) == 1
    assert board.get(task_id).state == DONE


def test_rejected_approval_marks_only_its_waiting_task_failed():
    board = TaskBoard(":memory:")
    first = board.enqueue("tool", {"tool": "deploy"})
    second = board.enqueue("tool", {"tool": "message"})
    assert board.claim(first) and board.claim(second)
    assert board.await_approval(first, "approval-a")
    assert board.await_approval(second, "approval-b")
    assert board.resolve_approval("approval-a", approved=False, error="denied") == 1
    assert board.get(first).state == FAILED
    assert board.get(first).last_error == "denied"
    assert board.get(second).state == AWAITING_APPROVAL
