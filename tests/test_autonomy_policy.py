"""Contract tests for the deterministic, evidence-only autonomy policy."""
from hive.autonomy.policy import (
    AutonomyPolicyStore,
    POLICY_VERSION,
    PolicyAction,
    evaluate_edit,
    policy_catalog,
)
from hive.core.spec_search import EditOp, EditOutcome, RiskTier


def test_every_edit_operation_has_one_deterministic_policy_action():
    catalog = policy_catalog()

    assert set(catalog) == {op.value for op in EditOp}
    assert catalog[EditOp.ADD_TEST.value] == PolicyAction.AUTOMATIC.value
    assert catalog[EditOp.PATCH_CODE.value] == PolicyAction.OWNER_APPROVAL.value
    assert catalog[EditOp.DEPENDENCY_CHANGE.value] == PolicyAction.NOTIFY_ONLY.value


def test_unknown_or_protected_actions_fail_closed_without_an_escalation_input():
    assert evaluate_edit("made_up_action").action is PolicyAction.DENY
    protected = evaluate_edit(EditOp.ADD_TEST, target_files=("Config/SOUL.md",))
    assert protected.action is PolicyAction.DENY
    assert "never policy-authorized" in protected.reason


def test_policy_evidence_is_append_only_and_never_used_to_change_classification(tmp_path):
    path = tmp_path / "state.sqlite"
    store = AutonomyPolicyStore(path, clock=lambda: 123.0)
    review = evaluate_edit(EditOp.PATCH_CODE)

    assert store.record("edit-1", review) is True
    assert store.record("edit-1", evaluate_edit(EditOp.ADD_TEST)) is False
    reopened = AutonomyPolicyStore(path, clock=lambda: 124.0)

    summary = reopened.summary()
    assert summary["policy_version"] == POLICY_VERSION
    assert summary["learning_mode"] == "evidence_only_never_escalates"
    assert summary["decision_counts"]["owner_approval"] == 1
    assert evaluate_edit(EditOp.PATCH_CODE).action is PolicyAction.OWNER_APPROVAL
    assert "idempotency_key" not in repr(summary)


def test_policy_outcome_keeps_target_aware_denial_separate_from_runtime_status(tmp_path):
    store = AutonomyPolicyStore(tmp_path / "state.sqlite", clock=lambda: 123.0)
    outcome = EditOutcome(
        edit_id="protected-edit", op=EditOp.ADD_TEST, tier=RiskTier.REVIEW,
        status="escalated_safety", target_files=("Config/SOUL.md",),
    )

    assert store.record_outcome("edit:proposal", outcome) is True
    assert store.record_outcome("edit:proposal", outcome) is False

    summary = store.summary()
    assert summary["decision_counts"]["deny"] == 1
    assert summary["decision_counts"]["automatic"] == 0
    assert summary["outcome_counts"] == {"escalated_safety": 1}
    assert "Config/SOUL.md" not in repr(summary)
