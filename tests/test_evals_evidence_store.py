from __future__ import annotations

import pytest

from hive.evals.evidence_store import EvaluationEvidenceStore


def test_evidence_store_retains_only_aggregate_metadata_and_fresh_pass(tmp_path):
    store = EvaluationEvidenceStore(tmp_path / "state.sqlite", clock=lambda: 100.0)
    evidence = store.record(
        suite_id="telegram-safe-learning", suite_version=1, manifest_digest="a" * 64,
        total=5, passed=5, failed=0, errored=0, offline_only=True,
        started_ts=90.0, finished_ts=100.0,
    )
    assert evidence.all_passed is True
    assert store.has_fresh_pass("telegram-safe-learning", 1, max_age_seconds=10, now=110.0)
    assert not store.has_fresh_pass("telegram-safe-learning", 1, max_age_seconds=9, now=110.0)


def test_evidence_store_rejects_inconsistent_summary(tmp_path):
    store = EvaluationEvidenceStore(tmp_path / "state.sqlite")
    with pytest.raises(ValueError, match="summary"):
        store.record(
            suite_id="suite", suite_version=1, manifest_digest="digest", total=2,
            passed=2, failed=1, errored=0, offline_only=True, started_ts=1.0,
        )


def test_failed_or_non_offline_evidence_never_opens_gate(tmp_path):
    store = EvaluationEvidenceStore(tmp_path / "state.sqlite", clock=lambda: 10.0)
    store.record(suite_id="suite", suite_version=1, manifest_digest="digest", total=1,
                 passed=1, failed=0, errored=0, offline_only=False, started_ts=1.0)
    assert not store.has_fresh_pass("suite", 1, max_age_seconds=100)
