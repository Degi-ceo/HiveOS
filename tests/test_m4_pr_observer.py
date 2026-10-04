"""M4 GitHub PR observation is read-only and persistence-safe."""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from hive.core.pr_observer import GitHubPRObserver, classify_pr
from hive.observability.persistence import ObservabilityLedger


def test_classifies_failed_checks_before_review_state():
    result = classify_pr(
        {"number": 7, "state": "open", "html_url": "https://example/pr/7", "head": {"sha": "abc"}},
        [{"head_sha": "abc", "status": "completed", "conclusion": "failure"}],
        [{"state": "APPROVED"}],
    )
    assert result.status == "checks_failed"
    assert result.ci_state == "failed"
    assert "APPROVED" not in str(result.as_dict())


def test_observer_uses_only_get_paths_and_labels_review_body_untrusted():
    paths: list[str] = []

    async def fetch(path):
        paths.append(path)
        if path.endswith("/status"):
            return {"sha": "abc", "state": "pending", "total_count": 0, "statuses": []}
        if "/issues/9/comments" in path:
            return []
        if "/pulls/9/comments" in path:
            return []
        if path.split("?", 1)[0].endswith("/reviews"):
            return [{"state": "CHANGES_REQUESTED", "body": "secret review text"}]
        if "check-runs" in path:
            return {"total_count": 1, "check_runs": [
                {"head_sha": "abc", "status": "completed", "conclusion": "success"},
            ]}
        return {"number": 9, "state": "open", "html_url": "https://example/pr/9", "head": {"sha": "abc"}}

    observed = asyncio.run(GitHubPRObserver("token", "owner", "repo", fetcher=fetch).observe(9))
    assert observed.status == "changes_requested"
    assert observed.ci_state == "passed"
    assert len(paths) == 6
    assert all(path.startswith("/repos/owner/repo/") for path in paths)
    assert observed.as_dict()["review_notes"] == [
        {"kind": "review", "id": 0,
         "body": {"trust": "untrusted", "text": "secret review text"}},
    ]


def test_classifies_latest_review_state_per_reviewer():
    result = classify_pr(
        {"number": 7, "state": "open", "html_url": "https://example/pr/7", "head": {"sha": "abc"}},
        [],
        [
            {"state": "CHANGES_REQUESTED", "user": {"id": 1}, "submitted_at": "2026-01-01T00:00:00Z"},
            {"state": "APPROVED", "user": {"id": 1}, "submitted_at": "2026-01-02T00:00:00Z"},
        ],
    )
    assert result.status == "incomplete_evidence"
    assert result.ci_state == "unknown"
    assert result.review_state == "approved"


def test_get_only_observation_includes_identity_classic_status_and_issue_feedback():
    paths: list[str] = []

    async def fetch(path):
        paths.append(path)
        if path == "/repos/owner/repo/pulls/17":
            return {
                "id": 1700, "number": 17, "state": "open", "draft": False,
                "html_url": "https://github.com/owner/repo/pull/17",
                "user": {"id": 51},
                "head": {"sha": "abc", "ref": "hive/auto-17", "repo": {"id": 101}},
                "base": {"ref": "main", "repo": {"id": 101}},
            }
        if "check-runs" in path:
            return {"total_count": 1, "check_runs": [
                {"name": "unit", "head_sha": "abc", "status": "completed",
                 "conclusion": "success"},
            ]}
        if path.endswith("/status"):
            return {"sha": "abc", "state": "failure", "total_count": 1,
                    "statuses": [{"context": "legacy/unit", "state": "failure"}]}
        if "/reviews?" in path:
            return [{"id": 71, "state": "COMMENTED", "body": "review note",
                     "user": {"id": 52}, "submitted_at": "2026-01-01T00:00:00Z"}]
        if "/pulls/17/comments?" in path:
            return [{"id": 72, "body": "change this line", "user": {"id": 53},
                     "path": "src/hive/example.py", "commit_id": "abc",
                     "created_at": "2026-01-02T00:00:00Z"}]
        if "/issues/17/comments?" in path:
            return [{"id": 73, "body": "general note", "user": {"id": 54},
                     "created_at": "2026-01-03T00:00:00Z"}]
        raise AssertionError(path)

    result = asyncio.run(GitHubPRObserver("token", "owner", "repo", fetcher=fetch).observe(17))
    assert result.ci_state == "failed"
    assert result.status == "checks_failed"
    assert (result.pr_id, result.author_id, result.head_repo_id, result.head_ref,
            result.base_repo_id, result.base_ref) == (1700, 51, 101, "hive/auto-17", 101, "main")
    assert any(note["kind"] == "inline" and note["author_id"] == 53
               and note["path"] == "src/hive/example.py" and note["commit_id"] == "abc"
               for note in result.review_notes)
    assert any(note["kind"] == "issue" and note["author_id"] == 54
               for note in result.review_notes)
    assert all(note["body"]["trust"] == "untrusted" for note in result.review_notes)
    assert len(paths) == 6
    assert all(path.startswith("/repos/owner/repo/") for path in paths)


def test_ci_state_is_separate_from_draft_and_zero_evidence_is_not_green():
    pr = {"number": 3, "state": "open", "draft": True, "head": {"sha": "abc"}}
    failed = classify_pr(pr, [{"head_sha": "abc", "status": "completed",
                               "conclusion": "failure"}], [])
    assert failed.status == "draft"
    assert failed.ci_state == "failed"
    assert failed.as_dict()["draft"] is True
    empty = classify_pr({**pr, "draft": False}, [], [],
                        commit_status={"sha": "abc", "state": "pending",
                                       "total_count": 0, "statuses": []})
    assert empty.ci_state == "unknown"
    assert empty.status != "ready_for_human_merge"


def test_classic_status_alone_can_report_failure_only_for_current_head():
    pr = {"number": 3, "state": "open", "head": {"sha": "abc"}}
    classic = {"sha": "abc", "state": "failure", "total_count": 1,
               "statuses": [{"context": "legacy/test", "state": "error"}]}
    failed = classify_pr(pr, [], [], commit_status=classic)
    assert failed.ci_state == "failed"
    assert failed.status == "checks_failed"
    stale = classify_pr(pr, [], [], commit_status={**classic, "sha": "def"})
    assert stale.ci_state == "incomplete"


def test_pending_rerun_prevents_stale_failure_from_becoming_actionable():
    result = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": "abc"}},
        [{"name": "unit", "head_sha": "abc", "status": "completed",
          "conclusion": "failure"},
         {"name": "unit", "head_sha": "abc", "status": "in_progress",
          "conclusion": None}], [],
        commit_status={"sha": "abc", "state": "pending", "total_count": 1,
                       "statuses": [{"context": "legacy", "state": "pending"}]},
    )
    assert result.ci_state == "pending"
    assert result.status == "checks_pending"


def test_same_suite_name_cannot_prove_an_old_failure_was_superseded():
    identity = {"name": "unit", "head_sha": "abc",
                "app": {"id": 7}, "check_suite": {"id": 8}}
    result = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": "abc"}},
        [{**identity, "id": 10, "status": "completed", "conclusion": "failure"},
         {**identity, "id": 11, "status": "completed", "conclusion": "success"}],
        [],
    )
    assert result.ci_state == "failed"
    assert result.checks_failed == 1
    assert result.status == "checks_failed"


def test_incomplete_issue_page_and_mismatched_check_sha_fail_closed():
    pr = {"number": 3, "state": "open", "head": {"sha": "abc"}}
    status = {"sha": "abc", "state": "success", "total_count": 1,
              "statuses": [{"context": "legacy", "state": "success"}]}
    mismatch = classify_pr(pr, [{"status": "completed", "conclusion": "success",
                                 "head_sha": "different"}], [], commit_status=status)
    assert mismatch.ci_state == "incomplete"
    assert mismatch.status == "incomplete_evidence"
    page = classify_pr(pr, [], [], issue_comments=[{"id": i, "body": "note"}
                                                      for i in range(100)],
                       commit_status=status, incomplete=True)
    assert page.ci_state == "incomplete"
    assert len(page.review_notes) <= 10


def test_observer_marks_full_issue_page_and_invalid_classic_status_incomplete():
    classic = {"sha": "abc", "state": "success", "total_count": 0, "statuses": []}

    async def fetch(path):
        if path.endswith("/pulls/3"):
            return {"number": 3, "state": "open", "head": {"sha": "abc"}}
        if "check-runs" in path:
            return {"total_count": 1, "check_runs": [
                {"status": "completed", "conclusion": "success"},
            ]}
        if path.endswith("/status"):
            return classic
        if "/issues/3/comments?" in path:
            return [{"id": i, "body": "note"} for i in range(100)]
        return []

    result = asyncio.run(GitHubPRObserver("token", "owner", "repo", fetcher=fetch).observe(3))
    assert result.ci_state == "incomplete"
    assert result.status == "incomplete_evidence"
    assert len(result.review_notes) <= 10

    classic["state"] = "pending"
    classic["statuses"] = ["not a status"]
    classic["total_count"] = 1
    malformed = classify_pr({"number": 3, "state": "open", "head": {"sha": "abc"}},
                            [], [], commit_status=classic)
    assert malformed.ci_state == "incomplete"


def test_issue_feedback_not_starved_by_review_history():
    result = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": "abc"}}, [],
        [{"id": i, "body": f"old review {i}"} for i in range(20)],
        issue_comments=[{"id": 99, "body": "current issue feedback"}],
    )
    assert any(note["kind"] == "issue" and note["id"] == 99
               for note in result.review_notes)
    assert len(result.review_notes) <= 10


def test_observer_redacts_and_bounds_untrusted_provenance(monkeypatch):
    marker = "demo-secret-credential-56789"
    monkeypatch.setenv("HIVE_OBSERVER_TEST_TOKEN", marker)
    observation = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": "abc"}},
        [{"name": f"unit-{marker}", "status": "completed", "conclusion": "success"}],
        [], [{"id": 8, "user": {"id": 9}, "path": f"src/{marker}.py",
              "commit_id": "abc", "body": f"{marker} " + "é" * 1000}],
    )
    rendered = str(observation.as_dict())
    assert marker not in rendered
    assert observation.review_notes[0]["body"]["trust"] == "untrusted"
    assert len(observation.review_notes[0]["body"]["text"].encode("utf-8")) <= 500


def test_malformed_head_sha_never_becomes_an_api_path():
    paths: list[str] = []

    async def fetch(path):
        paths.append(path)
        return {"number": 3, "state": "open", "head": {"sha": "abc/../../user"}}

    result = asyncio.run(GitHubPRObserver("token", "owner", "repo", fetcher=fetch).observe(3))
    assert result.ci_state == "incomplete"
    assert result.head_sha == ""
    assert paths == ["/repos/owner/repo/pulls/3"]


def test_config_only_secret_is_omitted_from_issue_body_and_inline_path():
    secret = "local/config-only-approver-56789"
    encoded = secret
    for _ in range(5):
        encoded = quote(encoded, safe="")

    result = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": "abc"},
         "body": f"PR body {encoded}"},
        [{"name": f"unit-{encoded}", "status": "completed", "conclusion": "success"}],
        [], [{"id": 1, "body": "inline", "path": f"src/{encoded}.py"}],
        issue_comments=[{"id": 2, "body": f"review: {encoded}"}],
        secret_values=[secret],
    )
    rendered = str(result.as_dict())
    assert secret not in rendered
    assert encoded not in rendered
    assert result.pr_body == {"trust": "untrusted", "text": "[omitted credential-bearing PR evidence]"}
    assert result.review_notes[0]["path"] == "[omitted credential-bearing PR evidence]"
    assert result.review_notes[1]["body"] == {
        "trust": "untrusted", "text": "[omitted credential-bearing PR evidence]",
    }


def test_missing_check_or_combined_status_sha_is_incomplete():
    from hive.core.pr_observer import classify_pr

    pr = {"number": 3, "state": "open", "head": {"sha": "a" * 40}}
    failed = [{"name": "unit", "status": "completed", "conclusion": "failure"}]
    current_status = {"sha": "a" * 40, "total_count": 0, "statuses": [],
                      "state": "pending"}
    missing_check_sha = classify_pr(pr, failed, [], commit_status=current_status)
    assert missing_check_sha.ci_state == "incomplete"
    current_failed = [{**failed[0], "head_sha": "a" * 40}]
    missing_status_sha = classify_pr(
        pr, current_failed, [], commit_status={
            "total_count": 1, "statuses": [{"state": "failure"}],
            "state": "failure",
        },
    )
    assert missing_status_sha.ci_state == "incomplete"


def test_same_named_check_runs_do_not_hide_a_failure():
    from hive.core.pr_observer import classify_pr

    sha = "a" * 40
    common = {"name": "unit", "head_sha": sha, "status": "completed",
              "app": {"id": 1}, "check_suite": {"id": 2}}
    checks = [
        {**common, "id": 10, "conclusion": "failure"},
        {**common, "id": 11, "conclusion": "success"},
    ]
    observed = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": sha}}, checks,
        [{"state": "APPROVED", "user": {"id": 1}}],
    )
    assert observed.ci_state == "failed"
    assert observed.status == "checks_failed"
    assert observed.checks_failed == 1


def test_pending_classic_context_prevents_feedback_repair():
    from hive.core.pr_observer import classify_pr

    sha = "a" * 40
    observed = classify_pr(
        {"number": 3, "state": "open", "head": {"sha": sha}}, [], [],
        commit_status={"sha": sha, "total_count": 2,
                       "statuses": [{"state": "failure"}, {"state": "pending"}],
                       "state": "failure"},
    )
    assert observed.ci_state == "pending"
    assert observed.status == "checks_pending"


def test_observer_passes_local_secret_values_without_global_registration():
    secret = "observer-config-only-key-98765"

    async def fetch(path):
        if path.endswith("/pulls/3"):
            return {"number": 3, "state": "open", "head": {"sha": "abc"}}
        if "check-runs" in path:
            return {"total_count": 0, "check_runs": []}
        if path.endswith("/status"):
            return {"sha": "abc", "state": "pending", "total_count": 0, "statuses": []}
        if "/issues/3/comments?" in path:
            return [{"id": 2, "body": f"please use {secret}"}]
        return []

    result = asyncio.run(GitHubPRObserver(
        "token", "owner", "repo", fetcher=fetch, secret_values=[secret],
    ).observe(3))
    assert result.review_notes[0]["body"]["text"] == (
        "[omitted credential-bearing PR evidence]"
    )
    assert secret not in str(result.as_dict())


def test_secret_shaped_head_sha_is_not_echoed_or_used_as_api_path():
    secret = "abcdef0123456789abcdef0123456789abcdef01"
    paths: list[str] = []

    async def fetch(path):
        paths.append(path)
        return {"number": 3, "state": "open", "head": {"sha": secret}}

    result = asyncio.run(GitHubPRObserver(
        "token", "owner", "repo", fetcher=fetch, secret_values=[secret],
    ).observe(3))
    assert result.ci_state == "incomplete"
    assert result.head_sha == ""
    assert paths == ["/repos/owner/repo/pulls/3"]


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
        available = True

        async def observe(self, _number):
            return classify_pr(
                {"number": 3, "state": "open", "html_url": "https://example/pr/3", "head": {"sha": "abc"}},
                [], [],
            )

    from hive.runtime import HiveOS
    from unittest.mock import MagicMock

    hive = MagicMock(spec=HiveOS)
    hive.config = type("Config", (), {"github_owner": "owner", "github_repo": "repo"})()
    hive.pr_observer = _Observer()
    hive.observability_ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    hive.observability_ledger.record_selfmod({
        "run_id": "run-3", "title": "candidate",
        "pr_url": "https://github.com/owner/repo/pull/3", "ok": True, "stage": "pushed",
    })
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
