import pytest

from hive.core.selfdev_store import SelfDevelopmentStore


def test_proposals_are_durable_and_redact_symptom(tmp_path):
    db = tmp_path / "state.sqlite"
    first = SelfDevelopmentStore(db, clock=lambda: 10.0).propose(
        symptom="token=private must never be stored", plan="inspect failing test", rationale="regression",
    )
    reopened = SelfDevelopmentStore(db, clock=lambda: 20.0)
    item = reopened.get(first.run_id)

    assert item.state == "requires_review"
    assert item.symptom_digest != "token=private must never be stored"
    assert len(item.symptom_digest) == 64
    assert reopened.recent() == [item]


def test_evidence_transition_is_once_only(tmp_path):
    store = SelfDevelopmentStore(tmp_path / "state.sqlite")
    item = store.propose(symptom="failure", plan="run focused tests", rationale="evidence")
    complete = store.record_evidence(item.run_id, state="draft_pr_opened",
                                     branch="hive/selfdev-1", pr_url="https://example.com/pr/1",
                                     test_summary="focused: passed", lesson="retain regression")

    assert complete.state == "draft_pr_opened"
    with pytest.raises(ValueError):
        store.record_evidence(item.run_id, state="rejected")
def test_evidence_can_be_resolved_by_durable_approval_id(tmp_path):
    store = SelfDevelopmentStore(tmp_path / "state.sqlite")
    item = store.propose(symptom="failure", plan="review", rationale="evidence", approval_id="approval-1")

    resolved = store.record_evidence_for_approval("approval-1", state="rejected", lesson="owner rejected")

    assert resolved is not None and resolved.run_id == item.run_id
    assert resolved.state == "rejected"
    assert store.record_evidence_for_approval("unknown", state="rejected") is None
