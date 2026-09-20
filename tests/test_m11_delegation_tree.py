"""M11 — durable, locally fenced nested delegation authority."""
from __future__ import annotations

import threading

import pytest

from hive.agents.delegations import COMPLETED, RUNNING, DelegationLedger


def _claimed_coordinator(tmp_path) -> tuple[DelegationLedger, str]:
    ledger = DelegationLedger(
        tmp_path / "state.sqlite", machine_identity="local-machine", hostname="local",
    )
    root = ledger.create(parent_run_id="root-run", child_run_id="coordinator-run", role="coordinator")
    assert ledger.claim(root.id) == 1
    return ledger, root.id


def test_coordinator_creates_only_a_bounded_local_leaf(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    child = ledger.create(
        parent_run_id="coordinator-run", child_run_id="research-run", role="researcher",
        parent_delegation_id=root_id,
    )

    assert child.parent_delegation_id == root_id
    assert child.root_delegation_id == root_id
    assert child.depth == 1
    assert child.target_machine_id == "local-machine"
    assert ledger.claim(child.id) == 1


def test_nested_delegation_fails_closed_without_machine_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_STATE_HOST_ID", raising=False)
    ledger = DelegationLedger(tmp_path / "state.sqlite", hostname="local")
    with pytest.raises(RuntimeError, match="HIVE_STATE_HOST_ID"):
        ledger.create(parent_run_id="root", child_run_id="child", role="coordinator")


def test_nested_delegation_cannot_expand_role_depth_or_child_budget(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    with pytest.raises(ValueError, match="child role"):
        ledger.create(parent_run_id="coordinator-run", child_run_id="other", role="coordinator",
                      parent_delegation_id=root_id)

    children = [
        ledger.create(parent_run_id="coordinator-run", child_run_id=f"child-{index}", role="researcher",
                      parent_delegation_id=root_id)
        for index in range(3)
    ]
    assert all(child.depth == 1 for child in children)
    with pytest.raises(ValueError, match="child budget"):
        ledger.create(parent_run_id="coordinator-run", child_run_id="overflow", role="reviewer",
                      parent_delegation_id=root_id)


def test_nested_delegation_reserves_one_bounded_branch_budget(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    root = ledger.get(root_id)
    assert root is not None
    assert (root.branch_max_turns, root.branch_reserved_turns) == (10, 4)

    children = [
        ledger.create(parent_run_id="coordinator-run", child_run_id=f"child-{index}", role="coder",
                      parent_delegation_id=root_id)
        for index in range(3)
    ]
    root = ledger.get(root_id)
    assert root is not None
    assert root.branch_reserved_turns == root.branch_max_turns == 10
    assert root.branch_reserved_tool_calls == root.branch_max_tool_calls == 10
    assert root.branch_reserved_seconds == root.branch_max_seconds == 110
    assert all(child.max_attempts == 1 for child in children)
    assert all(child.max_worker_turns == 2 for child in children)
    assert all("propose_candidate_file" not in child.granted_tools for child in children)


def test_nested_creation_is_atomic_and_rejects_another_owner_instance(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    other = DelegationLedger(tmp_path / "state.sqlite", machine_identity="local-machine", hostname="local")
    with pytest.raises(ValueError, match="locally owned"):
        other.create(parent_run_id="other", child_run_id="forged", role="researcher",
                     parent_delegation_id=root_id)
    other.close()

    barrier = threading.Barrier(12)
    results: list[object] = []

    def create_child(index: int) -> None:
        barrier.wait()
        try:
            results.append(ledger.create(
                parent_run_id="coordinator-run", child_run_id=f"thread-{index}", role="researcher",
                parent_delegation_id=root_id,
            ))
        except ValueError:
            results.append(None)

    threads = [threading.Thread(target=create_child, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([result for result in results if result is not None]) == 3
    assert len(ledger.children(root_id)) == 3


def test_nested_claims_respect_the_branch_concurrency_budget(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    first = ledger.create(parent_run_id="coordinator-run", child_run_id="first", role="researcher",
                          parent_delegation_id=root_id)
    second = ledger.create(parent_run_id="coordinator-run", child_run_id="second", role="reviewer",
                           parent_delegation_id=root_id)
    assert ledger.claim(first.id) == 1
    assert ledger.claim(second.id) is None
    assert ledger.finish(first.id, attempt=1, success=True)
    assert ledger.claim(second.id) == 1


def test_child_claim_refuses_foreign_machine_and_terminal_ancestor(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    child = ledger.create(parent_run_id="coordinator-run", child_run_id="child", role="researcher",
                          parent_delegation_id=root_id)
    foreign = DelegationLedger(tmp_path / "state.sqlite", machine_identity="foreign", hostname="foreign")
    assert foreign.claim(child.id) is None
    assert ledger.finish(root_id, attempt=1, success=True)
    assert ledger.get(root_id).state == COMPLETED
    assert ledger.claim(child.id) is None
    assert ledger.get(child.id).state != RUNNING
    foreign.close()


def test_child_claim_refuses_another_runtime_on_the_same_machine(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    child = ledger.create(parent_run_id="coordinator-run", child_run_id="child", role="researcher",
                          parent_delegation_id=root_id)
    other = DelegationLedger(tmp_path / "state.sqlite", machine_identity="local-machine", hostname="local")

    assert other.claim(child.id) is None
    assert ledger.claim(child.id) == 1
    assert ledger.finish(root_id, attempt=1, success=True)
    assert ledger.get(child.id).state == RUNNING
    other.close()


def test_public_tree_is_bounded_and_excludes_private_lineage_payloads(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    child = ledger.create(parent_run_id="private-parent-run", child_run_id="private-child-run", role="researcher",
                          parent_delegation_id=root_id)
    tree = ledger.tree(root_id, max_depth=4, max_nodes=10)

    assert tree is not None and tree["node_count"] == 2
    rendered = str(tree)
    assert child.id in rendered
    for private in ("private-parent-run", "private-child-run", "HIVE_APPROVER_KEY"):
        assert private not in rendered


def test_public_tree_never_exposes_terminal_summary(tmp_path):
    ledger, root_id = _claimed_coordinator(tmp_path)
    assert ledger.finish(root_id, attempt=1, success=True, summary="private diagnostic detail")

    tree = ledger.tree(root_id, max_depth=4, max_nodes=10)

    assert tree is not None
    assert "private diagnostic detail" not in str(tree)
