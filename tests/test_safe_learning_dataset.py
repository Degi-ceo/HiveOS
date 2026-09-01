from __future__ import annotations

from pathlib import Path

import pytest

from hive.evals.safe_learning import SUITE_ID, SUITE_VERSION, SafeLearningDatasetError, load_suite


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_safe_learning_suite_is_versioned_sanitized_and_covers_required_behaviours():
    items, digest = load_suite(REPO_ROOT)
    assert len(items) == 5
    assert len(digest) == 64
    assert {item.extra["scenario"] for item in items} == {
        "recall", "correction", "dangerous-refusal", "safe-task-plan", "polish",
    }
    assert all(item.extra["suite"] == SUITE_ID and item.extra["version"] == SUITE_VERSION for item in items)


def test_safe_learning_suite_rejects_forbidden_markers(tmp_path):
    dataset = tmp_path / "evals" / "datasets"
    dataset.mkdir(parents=True)
    (dataset / "telegram_safe_learning_v1.jsonl").write_text(
        '{"id":"x","input":"contains @","expected":"x","grader":"exact","extra":{"suite":"telegram-safe-learning","version":1,"scenario":"recall","offline_only":true}}\n',
        encoding="utf-8",
    )
    with pytest.raises(SafeLearningDatasetError, match="forbidden"):
        load_suite(tmp_path)
