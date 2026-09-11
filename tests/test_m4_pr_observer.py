"""M4 GitHub PR observation is read-only and persistence-safe."""
from __future__ import annotations

import asyncio

from hive.core.pr_observer import GitHubPRObserver, classify_pr
from hive.observability.persistence import ObservabilityLedger


def test_classifies_failed_checks_before_review_state():
    result = classify_pr(
        {"number": 7, "state": "open", "html_url": "https://example/pr/7", "head": {"sha": "abc"}},
        [{"status": "completed", "conclusion": "failure"}],
        [{"state": "APPROVED"}],
    )
    assert result.status == "checks_failed"
    assert "APPROVED" not in str(result.as_dict())


def test_observer_uses_only_get_paths_and_never_returns_review_body():
    paths: list[str] = []

    async def fetch(path):
        paths.append(path)
        if path.endswith("/reviews"):
            return [{"state": "CHANGES_REQUESTED", "body": "secret review text"}]
        if "check-runs" in path:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"number": 9, "state": "open", "html_url": "https://example/pr/9", "head": {"sha": "abc"}}

    observed = asyncio.run(GitHubPRObserver("token", "owner", "repo", fetcher=fetch).observe(9))
    assert observed.status == "changes_requested"
    assert all(path.startswith("/repos/owner/repo/") for path in paths)
    assert "secret review text" not in str(observed.as_dict())


def test_classifies_latest_review_state_per_reviewer():
    result = classify_pr(
        {"number": 7, "state": "open", "html_url": "https://example/pr/7", "head": {"sha": "abc"}},
        [],
        [
            {"state": "CHANGES_REQUESTED", "user": {"id": 1}, "submitted_at": "2026-01-01T00:00:00Z"},
            {"state": "APPROVED", "user": {"id": 1}, "submitted_at": "2026-01-02T00:00:00Z"},
        ],
    )
    assert result.status == "ready_for_human_merge"
    assert result.review_state == "approved"


def test_pr_observation_persists_safe_snapshot(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        ledger.record_pr_observation("run-1", {"number": 3, "url": "https://example/pr/3",
                                                 "status": "waiting_review", "checks_total": 2,
                                                 "checks_failed": 0, "checks_pending": 0,
                                                 "review_state": "waiting", "changes_requested": 0})
        assert ledger.pr_observations("run-1")[0]["status"] == "waiting_review"
    finally:
        ledger.close()


def test_pr_observation_keeps_only_latest_snapshot_and_clear_removes_it(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        ledger.record_selfmod({"run_id": "run-1", "title": "candidate"})
        ledger.record_pr_observation("run-1", {"number": 3, "url": "https://example/pr/3", "status": "waiting_review"})
        ledger.record_pr_observation("run-1", {"number": 3, "url": "https://example/pr/3", "status": "ready_for_human_merge"})
        assert [row["status"] for row in ledger.pr_observations("run-1")] == ["ready_for_human_merge"]
        ledger.clear_selfmod_history()
        assert ledger.pr_observations("run-1") == []
    finally:
        ledger.close()


def test_runtime_observation_persists_against_explicit_run(tmp_path):
    class _Observer:
        async def observe(self, _number):
            return classify_pr(
                {"number": 3, "state": "open", "html_url": "https://example/pr/3", "head": {"sha": "abc"}},
                [], [],
            )

    from hive.runtime import HiveOS
    from unittest.mock import MagicMock

    hive = MagicMock(spec=HiveOS)
    hive.pr_observer = _Observer()
    hive.observability_ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        result = asyncio.run(HiveOS.observe_selfmod_pr(hive, 3, run_id="run-3"))
        assert result["status"] == "waiting_review"
        assert hive.observability_ledger.pr_observations("run-3")
    finally:
        hive.observability_ledger.close()


def test_runtime_observes_only_recent_prs_from_its_configured_repository(tmp_path):
    from hive.runtime import HiveOS
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    hive = MagicMock(spec=HiveOS)
    hive.config = SimpleNamespace(github_owner="owner", github_repo="repo")
    hive.pr_observer.available = True
    hive.observability_ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    hive.observability_ledger.record_selfmod({
        "run_id": "run-good", "title": "safe", "pr_url": "https://github.com/owner/repo/pull/12",
        "ok": True, "stage": "pushed",
    })
    hive.observability_ledger.record_selfmod({
        "run_id": "run-other", "title": "ignore", "pr_url": "https://github.com/other/repo/pull/13",
        "ok": True, "stage": "pushed",
    })
    hive.observe_selfmod_pr = AsyncMock(return_value={"number": 12, "status": "waiting_review"})
    try:
        result = asyncio.run(HiveOS.observe_recent_selfmod_prs(hive))
        assert result == [{"number": 12, "status": "waiting_review"}]
        hive.observe_selfmod_pr.assert_awaited_once_with(12, run_id="run-good")
    finally:
        hive.observability_ledger.close()
