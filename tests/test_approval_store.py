from hive.core.approval_store import (
    APPROVED,
    EXECUTION_IN_PROGRESS,
    EXECUTION_REQUIRES_REVIEW,
    EXECUTION_SUCCEEDED,
    EXPIRED,
    KILLED,
    PENDING,
    ApprovalStore,
)


def test_independent_sqlite_connections_choose_one_approval_decision(tmp_path):
    db = tmp_path / "state.sqlite"
    first = ApprovalStore(db)
    second = ApprovalStore(db)
    assert first.record_pending("race-1", tool="deploy", args={}, reason="danger", kind="danger")

    assert first.decide("race-1", approved=True, decided_by="human:first") is True
    assert second.decide("race-1", approved=False, decided_by="human:second") is False
    record = second.get("race-1")
    assert record is not None and record.state == APPROVED and record.decided_by == "human:first"


def test_approval_store_survives_restart_and_decides_once(tmp_path):
    db = tmp_path / "state.sqlite"
    first = ApprovalStore(db)
    assert first.record_pending("a-1", tool="deploy", args={"target": "prod"},
                                reason="danger", kind="danger")
    assert not first.record_pending("a-1", tool="deploy", args={}, reason="x", kind="danger")
    first.close()

    restarted = ApprovalStore(db)
    record = restarted.get("a-1")
    assert record is not None and record.state == PENDING and record.args == {"target": "prod"}
    assert restarted.decide("a-1", approved=True, decided_by="human:web")
    assert not restarted.decide("a-1", approved=True, decided_by="human:web")
    assert restarted.get("a-1").state == APPROVED
    restarted.close()


def test_approval_store_expiry_never_approves(tmp_path):
    now = [100.0]
    store = ApprovalStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    assert store.record_pending("a-2", tool="deploy", args={}, reason="danger", kind="danger")
    now[0] = 200.0
    assert store.expire_before(150.0) == 1
    assert store.get("a-2").state == EXPIRED
    assert not store.decide("a-2", approved=True, decided_by="human:web")
    store.close()


def test_approval_store_kill_is_terminal_and_never_approves(tmp_path):
    store = ApprovalStore(tmp_path / "state.sqlite")
    assert store.record_pending("a-3", tool="deploy", args={}, reason="danger", kind="danger")
    assert store.kill("a-3")
    assert store.get("a-3").state == KILLED
    assert not store.decide("a-3", approved=True, decided_by="human:web")
    store.close()


def test_approval_store_persists_execution_intent_and_outcome(tmp_path):
    """Execution intent is durable before a protected action can start."""
    db = tmp_path / "state.sqlite"
    store = ApprovalStore(db)
    assert store.record_pending("a-4", tool="deploy", args={}, reason="danger", kind="danger")
    assert store.decide("a-4", approved=True, decided_by="human:web")

    assert store.begin_execution("a-4")
    assert store.get("a-4").execution_state == EXECUTION_IN_PROGRESS
    store.close()

    restarted = ApprovalStore(db)
    assert restarted.get("a-4").execution_state == EXECUTION_IN_PROGRESS
    recovered = restarted.recover_executions()
    assert len(recovered) == 1
    assert recovered[0].execution_state == EXECUTION_REQUIRES_REVIEW
    assert not restarted.finish_execution("a-4", succeeded=True)
    assert not restarted.begin_execution("a-4")
    restarted.close()


def test_approval_store_quarantines_an_unconfirmed_execution(tmp_path):
    store = ApprovalStore(tmp_path / "state.sqlite")
    assert store.record_pending("a-5", tool="deploy", args={}, reason="danger", kind="danger")
    assert store.decide("a-5", approved=True, decided_by="human:web")
    assert store.begin_execution("a-5")
    assert store.finish_execution("a-5", succeeded=False, error="transport lost")
    record = store.get("a-5")
    assert record.execution_state == EXECUTION_REQUIRES_REVIEW
    assert record.execution_error == "transport lost"
    store.close()


def test_restart_quarantines_approved_handoff_before_execution(tmp_path):
    """An approval cannot cross a process restart without an execution receipt."""
    db = tmp_path / "state.sqlite"
    first = ApprovalStore(db)
    assert first.record_pending("a-6", tool="deploy", args={}, reason="danger", kind="danger")
    assert first.decide("a-6", approved=True, decided_by="human:web")
    first.close()

    restarted = ApprovalStore(db)
    recovered = restarted.quarantine_approved_unstarted()
    assert [item.approval_id for item in recovered] == ["a-6"]
    record = restarted.get("a-6")
    assert record is not None and record.execution_state == EXECUTION_REQUIRES_REVIEW
    assert not restarted.begin_execution("a-6")


def test_durable_kill_switch_survives_restart_and_rejects_new_snapshots(tmp_path):
    db = tmp_path / "state.sqlite"
    first = ApprovalStore(db)
    assert first.record_pending("kill-1", tool="deploy", args={}, reason="danger", kind="danger")
    assert first.engage_kill_switch(actor="operator:test") == ["kill-1"]
    assert first.get("kill-1").state == KILLED
    first.close()

    restarted = ApprovalStore(db)
    assert restarted.is_killed() is True
    assert restarted.kill_state()["engaged_by"] == "operator:test"
    assert not restarted.record_pending("kill-2", tool="deploy", args={}, reason="danger", kind="danger")
    restarted.release_kill_switch(actor="operator:test")
    assert restarted.record_pending("kill-2", tool="deploy", args={}, reason="danger", kind="danger")


def test_durable_expiry_returns_only_terminalized_ids(tmp_path):
    now = [100.0]
    store = ApprovalStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    assert store.record_pending("old", tool="deploy", args={}, reason="danger", kind="danger")
    now[0] = 200.0
    assert store.expire_before_ids(150.0) == ["old"]
    assert store.get("old").state == EXPIRED
    assert store.expire_before_ids(150.0) == []
