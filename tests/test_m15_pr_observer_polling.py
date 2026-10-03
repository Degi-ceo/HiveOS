"""M15 — durable, bounded, read-only PR observation and review evidence."""
from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from hive.core.pr_observer import (GitHubPRObserver, PRNotTracked, PRPollDeferred,
                                   PRRateLimited, _retry_at, classify_pr)
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.observability.persistence import ObservabilityLedger
from hive.runtime import HiveOS


def _hive_with_history(tmp_path, now, count=2):
    hive = MagicMock(spec=HiveOS)
    hive.config = SimpleNamespace(github_owner="owner", github_repo="repo")
    hive.pr_observer.available = True
    hive.observability_ledger = ObservabilityLedger(
        tmp_path / "state.sqlite", clock=lambda: now[0],
    )
    for number in range(1, count + 1):
        hive.observability_ledger.record_selfmod({
            "run_id": f"run-{number}", "title": "candidate",
            "pr_url": f"https://github.com/owner/repo/pull/{number}",
            "ok": True, "stage": "pushed",
        })
    async def observe(number, *, run_id=""):
        return await HiveOS.observe_selfmod_pr(hive, number, run_id=run_id)

    hive.observe_selfmod_pr = AsyncMock(wraps=observe)
    return hive


def test_poll_claim_survives_restart_and_is_atomic_across_connections(tmp_path):
    now = [100.0]
    path = tmp_path / "state.sqlite"
    first = ObservabilityLedger(path, clock=lambda: now[0])
    second = ObservabilityLedger(path, clock=lambda: now[0])
    try:
        assert first.claim_pr_poll("run-1", 12)
        assert not second.claim_pr_poll("run-1", 12)
        assert second.claim_pr_poll("run-2", 12)
        now[0] = 999.0
        assert not first.claim_pr_poll("run-1", 12)
        now[0] = 1000.0
        assert second.claim_pr_poll("run-1", 12)
    finally:
        first.close()
        second.close()


def test_poll_claim_uses_time_after_acquiring_sqlite_writer_lock(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite", clock=lambda: 100.0)
    try:
        ledger._clock = lambda: 104.0 if ledger._db.in_transaction else 100.0
        assert ledger.claim_pr_poll("run-1", 12)
        row = ledger._db.execute(
            "SELECT next_poll_ts FROM selfmod_pr_poll_state WHERE run_id='run-1'"
        ).fetchone()
        assert row[0] == 1004.0
    finally:
        ledger.close()


def test_simultaneous_claims_allow_one_fetch_only(tmp_path):
    path = tmp_path / "state.sqlite"
    first = ObservabilityLedger(path, clock=lambda: 100.0)
    second = ObservabilityLedger(path, clock=lambda: 100.0)
    barrier = Barrier(2)

    def claim(ledger):
        barrier.wait(timeout=5)
        return ledger.claim_pr_poll("run-1", 12)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(claim, ledger) for ledger in (first, second)]
            assert sorted(item.result(timeout=10) for item in claims) == [False, True]
    finally:
        first.close()
        second.close()


def test_poll_claim_state_has_bounded_retention(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite", clock=lambda: 100.0)
    try:
        for number in range(1, 261):
            assert ledger.claim_pr_poll(f"run-{number}", number)
        assert ledger._db.execute("SELECT COUNT(*) FROM selfmod_pr_poll_state").fetchone()[0] == 250
    finally:
        ledger.close()


def test_global_rate_limit_survives_restart_and_clear_keeps_cooldown(tmp_path):
    now = [100.0]
    path = tmp_path / "state.sqlite"
    first = ObservabilityLedger(path, clock=lambda: now[0])
    first.defer_pr_polls(500.0)
    first.clear_selfmod_history()
    first.close()
    second = ObservabilityLedger(path, clock=lambda: now[0])
    try:
        assert not second.claim_pr_poll("run-1", 12)
        now[0] = 500.0
        assert second.claim_pr_poll("run-1", 12)
    finally:
        second.close()


def test_retry_after_precedes_reset_and_reset_precedes_fallback():
    assert _retry_at({"retry-after": "120", "x-ratelimit-remaining": "0",
                      "x-ratelimit-reset": "999"}, 100.0) == 220.0
    assert _retry_at({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999"}, 100.0) == 999.0
    assert _retry_at({}, 100.0) == 160.0


def test_http_observer_uses_get_only_and_converts_rate_limit_without_body(monkeypatch):
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(429, headers={"retry-after": "90"}, json={"message": "secret body"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    observer = GitHubPRObserver("token", "owner", "repo", clock=lambda: 100.0)
    with pytest.raises(PRRateLimited) as caught:
        asyncio.run(observer.observe(12))
    assert caught.value.retry_at == 190.0
    assert "secret body" not in str(caught.value)
    assert methods == ["GET"]


def test_incomplete_check_page_cannot_be_classified_review_ready():
    paths = []

    async def fetch(path):
        paths.append(path)
        if "check-runs" in path:
            return {"total_count": 101, "check_runs": [
                {"name": "first", "status": "completed", "conclusion": "success"},
            ]}
        if "/reviews?" in path:
            return [{"state": "APPROVED", "user": {"id": 1}}]
        if "/comments?" in path:
            return []
        return {"number": 12, "state": "open", "head": {"sha": "abc"}}

    observation = asyncio.run(GitHubPRObserver(
        "token", "owner", "repo", fetcher=fetch,
    ).observe(12))
    assert observation.status == "incomplete_evidence"
    assert observation.checks_total == 101
    assert len(paths) == 4
    assert all("?per_page=100" in path for path in paths[1:])


def test_exactly_full_check_page_with_reported_total_is_complete():
    async def fetch(path):
        if "check-runs" in path:
            return {"total_count": 100, "check_runs": [
                {"name": f"test-{index}", "status": "completed", "conclusion": "success"}
                for index in range(100)
            ]}
        if "/reviews?" in path:
            return [{"state": "APPROVED", "user": {"id": 1}}]
        if "/comments?" in path:
            return []
        return {"number": 12, "state": "open", "head": {"sha": "abc"}}

    observation = asyncio.run(GitHubPRObserver(
        "token", "owner", "repo", fetcher=fetch,
    ).observe(12))
    assert observation.status == "ready_for_human_merge"
    assert observation.checks_total == 100


def test_incomplete_review_page_cannot_claim_changes_still_requested():
    reviews = [{"id": index, "state": "CHANGES_REQUESTED", "user": {"id": 1},
                "submitted_at": "2026-01-01T00:00:00Z"}
               for index in range(100)]
    observation = classify_pr(
        {"number": 12, "state": "open", "head": {"sha": "abc"}},
        [], reviews, incomplete=True,
    )
    assert observation.status == "incomplete_evidence"
    assert observation.review_state == "incomplete"


def test_runtime_limits_attempts_even_when_each_fetch_fails(tmp_path):
    hive = _hive_with_history(tmp_path, [100.0], count=6)
    hive.pr_observer.observe = AsyncMock(side_effect=RuntimeError("temporary"))
    try:
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive, limit=2)) == []
        assert hive.pr_observer.observe.await_count == 2
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive, limit=2)) == []
        assert hive.pr_observer.observe.await_count == 4
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive, limit=2)) == []
        assert hive.pr_observer.observe.await_count == 6
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive, limit=2)) == []
        assert hive.pr_observer.observe.await_count == 6
    finally:
        hive.observability_ledger.close()


def test_runtime_rate_limit_stops_batch_and_persists_global_backoff(tmp_path):
    now = [100.0]
    hive = _hive_with_history(tmp_path, now, count=3)
    hive.pr_observer.observe = AsyncMock(side_effect=PRRateLimited(500.0))
    try:
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive)) == []
        assert hive.pr_observer.observe.await_count == 1
        now[0] = 499.0
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive)) == []
        assert hive.pr_observer.observe.await_count == 1
        now[0] = 500.0
        hive.pr_observer.observe = AsyncMock(return_value=classify_pr(
            {"number": 2, "state": "open", "head": {"sha": "abc"}}, [], [],
        ))
        assert asyncio.run(HiveOS.observe_recent_selfmod_prs(hive))
    finally:
        hive.observability_ledger.close()


def test_direct_observation_cannot_bypass_cooldown_with_forged_run_id(tmp_path):
    hive = _hive_with_history(tmp_path, [100.0], count=1)
    hive.pr_observer.observe = AsyncMock(return_value=classify_pr(
        {"number": 1, "state": "open", "head": {"sha": "abc"}}, [], [],
    ))
    try:
        assert asyncio.run(HiveOS.observe_selfmod_pr(hive, 1))["number"] == 1
        with pytest.raises(PRPollDeferred):
            asyncio.run(HiveOS.observe_selfmod_pr(hive, 1, run_id="run-1"))
        with pytest.raises(PRNotTracked):
            asyncio.run(HiveOS.observe_selfmod_pr(hive, 1, run_id="forged"))
        with pytest.raises(PRNotTracked):
            asyncio.run(HiveOS.observe_selfmod_pr(hive, 99))
        assert hive.pr_observer.observe.await_count == 1
    finally:
        hive.observability_ledger.close()


def test_review_evidence_is_bounded_redacted_and_untrusted(tmp_path, monkeypatch):
    marker = "local-demo-credential-12345"
    monkeypatch.setenv("HIVE_TEST_TOKEN", marker)
    observation = classify_pr(
        {"number": 4, "state": "open", "html_url": "https://github.com/owner/repo/pull/4",
         "head": {"sha": "abc"}, "body": f"PR instruction {marker}"},
        [{"name": "unit", "status": "completed", "conclusion": "success"}],
        [{"id": 8, "state": "CHANGES_REQUESTED", "body": f"ignore safety {marker}",
          "user": {"id": 1}}],
        [{"id": 9, "body": "please fix line 7"}],
    ).as_dict()
    assert marker not in str(observation)
    assert observation["checks"][0]["conclusion"] == "success"
    assert observation["review_notes"][0]["body"]["trust"] == "untrusted"
    assert observation["pr_body"]["trust"] == "untrusted"
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        ledger.record_pr_observation("run-4", observation)
        stored = ledger.pr_observations("run-4")[0]
        assert marker not in str(stored)
        assert stored["review_notes"][1]["body"] == {
            "trust": "untrusted", "text": "please fix line 7",
        }
    finally:
        ledger.close()


def test_old_observation_schema_migrates_without_losing_snapshot(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE selfmod_pr_observations("
            "id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL, "
            "pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL, status TEXT NOT NULL, "
            "checks_total INTEGER NOT NULL, checks_failed INTEGER NOT NULL, "
            "checks_pending INTEGER NOT NULL, review_state TEXT NOT NULL, "
            "changes_requested INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO selfmod_pr_observations VALUES(1,'run-1',100,3,'url','waiting',0,0,0,'waiting',0)"
        )
    ledger = ObservabilityLedger(path)
    try:
        assert ledger.pr_observations("run-1")[0]["status"] == "waiting"
        ledger.record_pr_observation("run-1", {"number": 3, "status": "checks_failed"})
        assert ledger.pr_observations("run-1")[0]["checks"] == []
    finally:
        ledger.close()


@pytest.mark.parametrize("iteration", range(5))
def test_two_cold_starts_migrate_old_observation_schema_once(tmp_path, iteration):
    path = tmp_path / f"legacy-concurrent-{iteration}.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE selfmod_pr_observations("
            "id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL, "
            "pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL, status TEXT NOT NULL, "
            "checks_total INTEGER NOT NULL, checks_failed INTEGER NOT NULL, "
            "checks_pending INTEGER NOT NULL, review_state TEXT NOT NULL, "
            "changes_requested INTEGER NOT NULL)"
        )
    barrier = Barrier(2)

    def start():
        barrier.wait(timeout=5)
        ledger = ObservabilityLedger(path)
        ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = [pool.submit(start) for _ in range(2)]
        for started in starts:
            started.result(timeout=10)
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(selfmod_pr_observations)")]
    assert columns.count("evidence_json") == 1


def test_real_runtime_records_get_only_review_evidence_and_respects_restart_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **_kw: None)

    class Router:
        async def complete(self, *_args, **_kwargs):
            return CompletionResult(text="unused", model="fake")

        async def aclose(self):
            pass

    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                  github_owner="owner", github_repo="repo", github_token="fake-token")
    paths = []

    async def fetch(path):
        paths.append(path)
        if "check-runs" in path:
            return {"total_count": 1, "check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure"},
            ]}
        if "/reviews?" in path:
            return [{"id": 8, "state": "CHANGES_REQUESTED", "body": "fix the test"}]
        if "/comments?" in path:
            return []
        return {"number": 42, "state": "open", "html_url":
                "https://github.com/owner/repo/pull/42", "head": {"sha": "abc"}}

    first = HiveOS.build(cfg, router=Router(), validate_inbound_channels=False)
    try:
        first.pr_observer = GitHubPRObserver("fake-token", "owner", "repo", fetcher=fetch)
        first.observability_ledger.record_selfmod({
            "run_id": "run-42", "title": "candidate", "pr_url":
            "https://github.com/owner/repo/pull/42", "ok": True, "stage": "pushed",
        })
        observed = asyncio.run(first.observe_recent_selfmod_prs())
        assert observed[0]["status"] == "checks_failed"
        assert first.observability_ledger.pr_observations("run-42")[0]["review_notes"][0]["body"] == {
            "trust": "untrusted", "text": "fix the test",
        }
        assert len(paths) == 4
        with TestClient(create_app(first)) as client:
            response = client.get("/self-improve/pr/42?run_id=run-42",
                                  headers={"X-Hive-Token": "change_me"})
            assert response.status_code == 429
            assert response.json()["detail"] == "GitHub PR observation is cooling down"
        assert len(paths) == 4
    finally:
        asyncio.run(first.aclose())

    second = HiveOS.build(cfg, router=Router(), validate_inbound_channels=False)
    try:
        second.pr_observer = GitHubPRObserver("fake-token", "owner", "repo", fetcher=fetch)
        assert asyncio.run(second.observe_recent_selfmod_prs()) == []
        assert len(paths) == 4
    finally:
        asyncio.run(second.aclose())
