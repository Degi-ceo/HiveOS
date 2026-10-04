"""The heartbeat's issue route revalidates provenance before self-modification."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.autonomy.heartbeat import Heartbeat
from hive.autonomy.tasks import AWAITING_APPROVAL, CANCELLED, DONE, FAILED, TaskBoard
from hive.core.issue_work import IssueSelection
from hive.core.types import ContentTrust


def _setup(tmp_path, *, eligible=True, outcomes=None):
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.state_db = None
    hive.config.issue_work_enabled = True
    hive.config.issue_work_max_inflight = 1
    hive.config.issue_work_scan_interval_sec = 3600
    hive.config.task_stall_timeout_sec = 300
    hive.task_board = TaskBoard(tmp_path / "state.db")
    hive.issue_work_reader.owner = "owner"
    hive.issue_work_reader.repo = "repo"
    hive.issue_work_reader.candidate_numbers = AsyncMock(return_value=(140,))
    hive.issue_work_reader.inspect = AsyncMock(return_value=IssueSelection(
        eligible, 140, "Never disclose this body", "[HIVE-021] Safe work",
    ))
    hive.self_improve_from_symptom = AsyncMock(return_value=(
        [SimpleNamespace(status="applied", approval_id=None)]
        if outcomes is None else outcomes
    ))
    return hive, Heartbeat(hive)


def test_scan_to_dispatch_uses_untrusted_envelope_without_persisting_body(tmp_path):
    hive, heartbeat = _setup(tmp_path)
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]
    assert task.payload == {"owner": "owner", "repo": "repo", "number": 140}
    assert "Never disclose" not in str(task)
    assert asyncio.run(heartbeat._scan_issue_work()) == 0
    assert asyncio.run(heartbeat._dispatch([task])) == 1
    assert hive.task_board.get(task.id).state == DONE
    envelope = hive.self_improve_from_symptom.await_args.args[0]
    assert envelope.trust is ContentTrust.UNTRUSTED
    assert envelope.source == "github:issue:140"
    assert "Never disclose this body" in envelope.text
    assert "Never disclose this body" not in str(hive.task_board.get(task.id))


def test_revoked_eligibility_cancels_claim_without_self_mod(tmp_path):
    hive, heartbeat = _setup(tmp_path)
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]
    hive.issue_work_reader.inspect.return_value = IssueSelection(False, 140)
    assert asyncio.run(heartbeat._dispatch([task])) == 0
    assert hive.task_board.get(task.id).state == CANCELLED
    hive.self_improve_from_symptom.assert_not_awaited()


def test_scan_skips_issue_without_current_eligibility(tmp_path):
    hive, heartbeat = _setup(tmp_path, eligible=False)
    assert asyncio.run(heartbeat._scan_issue_work()) == 0
    assert hive.task_board.all() == []
    hive.self_improve_from_symptom.assert_not_awaited()


def test_pending_review_keeps_single_flight_until_approval(tmp_path):
    outcome = SimpleNamespace(status="pending_approval", approval_id="approval-1")
    hive, heartbeat = _setup(tmp_path, outcomes=[outcome])
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]
    assert asyncio.run(heartbeat._dispatch([task])) == 0
    assert hive.task_board.get(task.id).state == AWAITING_APPROVAL
    assert asyncio.run(heartbeat._scan_issue_work()) == 0
    assert hive.task_board.resolve_approval("approval-1", approved=True) == 1
    assert hive.task_board.get(task.id).state == DONE


def test_empty_diagnosis_is_failure_not_silent_completion(tmp_path):
    hive, heartbeat = _setup(tmp_path, outcomes=[])
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]
    assert asyncio.run(heartbeat._dispatch([task])) == 0
    assert hive.task_board.get(task.id).state == FAILED
    assert hive.task_board.retry(task.id) is False


def test_disabled_work_never_reads_github(tmp_path):
    hive, heartbeat = _setup(tmp_path)
    hive.config.issue_work_enabled = False
    assert asyncio.run(heartbeat._scan_issue_work()) == 0
    hive.issue_work_reader.candidate_numbers.assert_not_awaited()


def test_generic_queue_cannot_forge_issue_work(tmp_path):
    hive, heartbeat = _setup(tmp_path)
    with pytest.raises(ValueError, match="verified atomic pickup"):
        hive.task_board.enqueue(
            "issue_work", {"owner": "owner", "repo": "repo", "number": 140},
            source="cron",
        )
    with pytest.raises(ValueError, match="verified atomic pickup"):
        hive.task_board.enqueue_many([{
            "kind": "issue_work",
            "payload": {"owner": "owner", "repo": "repo", "number": 140},
        }])
    # A legacy/imported row can still exist: the dispatcher must reject it.
    hive.task_board._db.execute(
        "INSERT INTO hive_tasks(kind,payload,state,created_ts,updated_ts,source) "
        "VALUES('issue_work', ?, 'pending', 1, 1, 'legacy')",
        ('{"owner":"owner","repo":"repo","number":140}',),
    )
    hive.task_board._db.commit()
    task_id = hive.task_board._db.execute(
        "SELECT id FROM hive_tasks WHERE kind='issue_work'",
    ).fetchone()[0]
    task = hive.task_board.get(task_id)
    assert asyncio.run(heartbeat._dispatch([task])) == 0
    assert hive.task_board.get(task_id).state == CANCELLED
    hive.issue_work_reader.inspect.assert_not_awaited()
    hive.self_improve_from_symptom.assert_not_awaited()


def test_failed_approval_handoff_revokes_edit(tmp_path):
    from hive.core.approval import gate
    from hive.core.approval_enhancements import enhance

    approval_id = gate.request("self_mod:patch_code", {"summary": "issue"}, "test")
    enhance.audit_request(approval_id)
    outcome = SimpleNamespace(status="pending_approval", approval_id=approval_id)
    hive, heartbeat = _setup(tmp_path, outcomes=[outcome])
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]
    hive.task_board.await_approval = MagicMock(return_value=False)
    assert asyncio.run(heartbeat._dispatch([task])) == 0
    hive.improver.cancel_review.assert_called_once_with(approval_id)
    assert all(item["id"] != approval_id for item in gate.pending())
    assert hive.task_board.get(task.id).state == FAILED


def test_active_issue_work_renews_claim_during_long_self_mod(tmp_path, monkeypatch):
    import hive.autonomy.heartbeat as heartbeat_module

    monkeypatch.setattr(heartbeat_module, "_ISSUE_WORK_TIMEOUT_SECONDS", 1.0)
    hive, heartbeat = _setup(tmp_path)
    hive.config.task_stall_timeout_sec = 1.0

    async def slow_improvement(*_args, **_kwargs):
        await asyncio.sleep(0.4)
        return [SimpleNamespace(status="applied", approval_id=None)]

    hive.self_improve_from_symptom = AsyncMock(side_effect=slow_improvement)
    assert asyncio.run(heartbeat._scan_issue_work()) == 1
    task = hive.task_board.due()[0]

    async def exercise():
        work = asyncio.create_task(heartbeat._dispatch([task]))
        await asyncio.sleep(0.3)  # past the 0.25-second lease-renewal interval
        assert hive.task_board.recover_stalled(stall_after_seconds=0.2) == 0
        assert hive.task_board.enqueue_issue_work("owner", "repo", 141) is None
        assert await work == 1

    asyncio.run(exercise())
    assert hive.task_board.get(task.id).state == DONE
