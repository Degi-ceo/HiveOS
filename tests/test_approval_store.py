from hive.core.approval_store import APPROVED, EXPIRED, KILLED, PENDING, ApprovalStore


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
