"""Durable, fail-closed provenance for Hive-created pull requests."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hive.core.config import HiveConfig
from hive.core.self_mod import SelfModifier
from hive.observability.persistence import ObservabilityLedger
from hive.runtime import HiveOS


BRANCH = "hive/auto-owned-12345678"
SHA = "a" * 40
URL = "https://github.com/Degi-ceo/HiveOS/pull/42"


def _record(ledger: ObservabilityLedger, *, sha: str = SHA) -> None:
    ledger.record_selfmod({
        "run_id": "run-owned", "title": "candidate", "branch": BRANCH,
        "pr_url": URL, "head_sha": sha, "stage": "pushed", "ok": True,
    })


def _observation(**overrides) -> dict:
    result = {
        "number": 42, "url": URL, "state": "open", "head_sha": SHA,
        "pr_id": 5001, "author_id": 7001, "head_repo_id": 9001,
        "base_repo_id": 9001, "head_ref": BRANCH, "base_ref": "main",
    }
    result.update(overrides)
    return result


def test_created_pr_identity_binds_only_matching_observation_and_survives_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    ledger = ObservabilityLedger(path)
    try:
        _record(ledger)
        pending = ledger.get_pr_identity(URL)
        assert pending is not None and pending["bound"] is False
        assert pending["run_id"] == "run-owned"
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is True
    finally:
        ledger.close()

    restarted = ObservabilityLedger(path)
    try:
        bound = restarted.get_pr_identity(URL)
        assert bound is not None and bound["bound"] is True
        assert bound["pr_id"] == 5001
        assert bound["head_ref"] == BRANCH
    finally:
        restarted.close()


def test_pr_identity_rejects_changed_sha_branch_fork_and_author(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        _record(ledger)
        for changed in (
            {"head_sha": "b" * 40},
            {"head_ref": "human/branch"},
            {"head_repo_id": 9002},
            {"author_id": 0},
            {"base_ref": "other"},
            {"state": "closed"},
        ):
            assert ledger.bind_pr_identity("run-owned", URL, _observation(**changed)) is False
        assert ledger.bind_pr_identity("wrong-run", URL, _observation()) is False
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is True
        assert ledger.bind_pr_identity(
            "run-owned", URL, _observation(pr_id=5002),
        ) is False
    finally:
        ledger.close()


def test_legacy_pr_record_without_pushed_sha_is_never_bound(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        _record(ledger, sha="")
        assert ledger.get_pr_identity(URL) is None
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is False
    finally:
        ledger.close()


def test_clearing_selfmod_history_also_revokes_pr_identity(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is True
        assert ledger.clear_selfmod_history() == 1
        assert ledger.get_pr_identity(URL) is None
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is False
    finally:
        ledger.close()


def test_identity_table_migrates_existing_state_database(tmp_path):
    path = tmp_path / "old-state.sqlite"
    first = ObservabilityLedger(path)
    try:
        first.record_selfmod({"run_id": "legacy", "title": "old", "ok": True})
    finally:
        first.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE selfmod_pr_identity")
    reopened = ObservabilityLedger(path)
    try:
        assert reopened.selfmod_history()[0]["run_id"] == "legacy"
        _record(reopened)
        assert reopened.get_pr_identity(URL) is not None
    finally:
        reopened.close()


def test_pr_identity_race_can_bind_only_one_consistent_identity(tmp_path):
    path = tmp_path / "state.sqlite"
    seed = ObservabilityLedger(path)
    try:
        _record(seed)
    finally:
        seed.close()

    def bind(pr_id: int) -> bool:
        ledger = ObservabilityLedger(path)
        try:
            return ledger.bind_pr_identity(
                "run-owned", URL, _observation(pr_id=pr_id),
            )
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, (5001, 5002)))
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_self_modifier_records_exact_pushed_sha_for_future_pr_verification(tmp_path):
    from tests.test_self_mod import _apply_ok, _runner

    basic_run = _runner()
    async def run(command, cwd=None):
        if command == ["git", "rev-parse", "HEAD"] and cwd != str(tmp_path):
            return 0, SHA + "\n"
        return await basic_run(command, cwd)

    async def open_pr(_branch, _title, _body):
        return URL

    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        result = asyncio.run(SelfModifier(
            repo_root=str(tmp_path), run=run, open_pr=open_pr,
            history_store=ledger,
        ).propose(
            "candidate", "", _apply_ok, approved_review=True,
            run_id="run-owned",
        ))
        assert result["stage"] == "pushed"
        assert result["head_sha"] == SHA
        identity = ledger.get_pr_identity(URL)
        assert identity is not None
        assert identity["branch"] == result["branch"]
        assert identity["pushed_sha"] == SHA
    finally:
        ledger.close()


def test_runtime_observation_binds_only_proven_pr_identity(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        _record(ledger)
        hive = MagicMock(spec=HiveOS)
        hive.config = SimpleNamespace(github_owner="Degi-ceo", github_repo="HiveOS")
        hive.observability_ledger = ledger
        hive.pr_observer.available = True
        hive.pr_observer.observe = AsyncMock(
            return_value=SimpleNamespace(as_dict=lambda: _observation()),
        )
        result = asyncio.run(HiveOS.observe_selfmod_pr(hive, 42, run_id="run-owned"))
        assert result["ownership_verified"] is True
        assert ledger.get_pr_identity(URL)["bound"] is True
    finally:
        ledger.close()


def test_runtime_passes_config_only_secrets_to_pr_observer(tmp_path, monkeypatch):
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **_kw: None)
    secret = "config-only-approver-for-observer-45981"
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        approver_key=secret, github_token="test-token",
        github_owner="owner", github_repo="repo",
    )
    router = SimpleNamespace(aclose=AsyncMock())
    hive = HiveOS.build(cfg, router=router, validate_inbound_channels=False)
    try:
        async def fetch(path):
            if path.endswith("/pulls/3"):
                return {"number": 3, "state": "open", "head": {"sha": "abc"},
                        "body": f"report {secret}"}
            if "check-runs" in path:
                return {"total_count": 0, "check_runs": []}
            if path.endswith("/status"):
                return {"sha": "abc", "total_count": 0, "statuses": [],
                        "state": "pending"}
            return []

        hive.pr_observer._fetcher = fetch
        observed = asyncio.run(hive.pr_observer.observe(3)).as_dict()
        assert observed["pr_body"] == {
            "trust": "untrusted", "text": "[omitted credential-bearing PR evidence]",
        }
        assert secret not in str(observed)
    finally:
        asyncio.run(hive.aclose())


def test_durable_pr_snapshot_keeps_safe_ci_and_comment_provenance(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        ledger.record_pr_observation("run-owned", {
            "number": 42, "url": URL, "status": "checks_failed",
            "ci_state": "failed", "ownership_verified": True,
            "review_notes": [{
                "kind": "inline", "id": 81, "author_id": 501,
                "path": "src/hive/core/example.py", "commit_id": SHA,
                "created_at": "2026-10-04T10:00:00Z",
                "body": {"trust": "untrusted", "text": "Please adjust the guard."},
            }],
        })
        snap = ledger.pr_observations("run-owned")[0]
        assert snap["ci_state"] == "failed"
        assert snap["ownership_verified"] is True
        assert snap["review_notes"][0] == {
            "kind": "inline", "id": 81, "author_id": 501,
            "path": "src/hive/core/example.py", "commit_id": SHA,
            "created_at": "2026-10-04T10:00:00Z",
            "body": {"trust": "untrusted", "text": "Please adjust the guard."},
        }
    finally:
        ledger.close()
