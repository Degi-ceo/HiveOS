"""Spawn-process evidence for durable approval decisions on Windows."""
from __future__ import annotations

import multiprocessing
from pathlib import Path

from hive.core.approval_store import APPROVED, REJECTED, ApprovalStore


def _decide_in_fresh_process(
    database: str,
    approval_id: str,
    approved: bool,
    start: object,
    ready: object,
    results: object,
) -> None:
    """Race an independent SQLite connection after the parent opens the gate."""
    store = ApprovalStore(database)
    ready.put(True)
    start.wait(timeout=10)
    results.put(store.decide(approval_id, approved=approved, decided_by=f"process:{approved}"))
    store.close()


def _begin_execution_in_fresh_process(database: str, approval_id: str, results: object) -> None:
    store = ApprovalStore(database)
    results.put(store.begin_execution(approval_id))
    store.close()


def _join_or_terminate(processes: list[object]) -> None:
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_two_spawned_processes_choose_exactly_one_approval_decision(tmp_path: Path) -> None:
    """A real process race leaves one durable, terminal owner decision."""
    database = str(tmp_path / "state.sqlite")
    parent = ApprovalStore(database)
    assert parent.record_pending("race-approval", tool="deploy", args={}, reason="danger", kind="danger")
    parent.close()

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(
            target=_decide_in_fresh_process,
            args=(database, "race-approval", approved, start, ready, results),
        )
        for approved in (True, False)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=10) is True
        assert ready.get(timeout=10) is True
        start.set()
        decisions = sorted(results.get(timeout=10) for _ in processes)
    finally:
        start.set()
        _join_or_terminate(processes)
    assert all(process.exitcode == 0 for process in processes)

    assert decisions == [False, True]
    recovered = ApprovalStore(database)
    record = recovered.get("race-approval")
    assert record is not None
    assert record.state in {APPROVED, REJECTED}
    assert record.decided_by in {"process:True", "process:False"}
    recovered.close()


def test_spawned_process_cannot_begin_execution_after_durable_emergency_stop(tmp_path: Path) -> None:
    """A process which did not receive the local stop signal still fails closed."""
    database = str(tmp_path / "state.sqlite")
    parent = ApprovalStore(database)
    assert parent.record_pending("stopped-approval", tool="deploy", args={}, reason="danger", kind="danger")
    assert parent.decide("stopped-approval", approved=True, decided_by="operator")
    assert parent.engage_kill_switch(actor="operator") == ["stopped-approval"]
    parent.close()

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_begin_execution_in_fresh_process,
        args=(database, "stopped-approval", results),
    )
    process.start()
    try:
        assert results.get(timeout=10) is False
    finally:
        _join_or_terminate([process])
    assert process.exitcode == 0

    recovered = ApprovalStore(database)
    record = recovered.get("stopped-approval")
    assert record is not None and record.state == "killed" and record.execution_state == "pending"
    recovered.close()
