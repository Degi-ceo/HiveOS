"""Durable, fail-closed provenance for Hive-created pull requests."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hive.core.config import HiveConfig
from hive.core.pr_feedback import repair_failed_ci_once, stand_down_once
from hive.core.pr_observer import PRObservation
from hive.core.self_mod import PRCreationReceipt, SelfModifier, github_pr_opener
from hive.observability.persistence import ObservabilityLedger
from hive.runtime import HiveOS


BRANCH = "hive/auto-owned-12345678"
SHA = "a" * 40
URL = "https://github.com/Degi-ceo/HiveOS/pull/42"


def _receipt(**overrides) -> dict:
    value = {
        "url": URL, "number": 42, "pr_id": 5001, "author_id": 7001,
        "head_repo_id": 9001, "base_repo_id": 9001,
        "head_ref": BRANCH, "head_sha": SHA, "base_ref": "main",
    }
    value.update(overrides)
    return value


def _record(ledger: ObservabilityLedger, *, sha: str = SHA,
            creation: dict | None = None) -> None:
    ledger.record_selfmod({
        "run_id": "run-owned", "title": "candidate", "branch": BRANCH,
        "pr_url": URL, "head_sha": sha, "stage": "pushed", "ok": True,
        "pr_creation": _receipt() if creation is None else creation,
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
        assert pending["created_author_id"] == 7001
        assert ledger.validate_pr_identity("run-owned", URL, _observation()) is False
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is True
        assert ledger.validate_pr_identity("run-owned", URL, _observation()) is True
    finally:
        ledger.close()

    restarted = ObservabilityLedger(path)
    try:
        bound = restarted.get_pr_identity(URL)
        assert bound is not None and bound["bound"] is True
        assert bound["pr_id"] == 5001
        assert bound["head_ref"] == BRANCH
        assert restarted.validate_pr_identity("run-owned", URL, _observation()) is True
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
            {"author_id": 7002},
            {"pr_id": 5002},
            {"base_repo_id": 9002},
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


def test_url_only_or_mismatched_receipt_cannot_authorize_writer(tmp_path):
    for index, creation in enumerate((
        {}, _receipt(author_id=7002), _receipt(head_sha="b" * 40),
    )):
        ledger = ObservabilityLedger(tmp_path / f"state-{index}.sqlite")
        try:
            _record(ledger, creation=creation)
            identity = ledger.get_pr_identity(URL)
            assert identity is not None and identity["bound"] is False
            if not creation or creation["head_sha"] != SHA:
                assert identity["created_author_id"] == 0
            assert ledger.bind_pr_identity("run-owned", URL, _observation()) is False
            assert ledger.validate_pr_identity("run-owned", URL, _observation()) is False
        finally:
            ledger.close()


def test_clearing_selfmod_history_also_revokes_pr_identity(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is True
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key="ci:failure:1",
        ) is not None
        assert ledger.finish_pr_feedback_round(URL, 1, state="failed")
        assert ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="round_cap",
        )
        assert ledger.clear_selfmod_history() == 1
        assert ledger.get_pr_identity(URL) is None
        assert ledger.bind_pr_identity("run-owned", URL, _observation()) is False
        assert ledger.get_pr_feedback_rounds(URL) == []
        assert ledger.get_pr_standdown(URL) is None
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


def test_old_identity_row_migrates_but_cannot_gain_author_from_first_get(tmp_path):
    path = tmp_path / "old-identity.sqlite"
    first = ObservabilityLedger(path)
    first.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE selfmod_pr_identity")
        connection.execute("""CREATE TABLE selfmod_pr_identity (
            pr_url TEXT PRIMARY KEY, run_id TEXT NOT NULL, pr_number INTEGER NOT NULL,
            branch TEXT NOT NULL, pushed_sha TEXT NOT NULL,
            pr_id INTEGER NOT NULL DEFAULT 0, author_id INTEGER NOT NULL DEFAULT 0,
            head_repo_id INTEGER NOT NULL DEFAULT 0, base_repo_id INTEGER NOT NULL DEFAULT 0,
            head_ref TEXT NOT NULL DEFAULT '', base_ref TEXT NOT NULL DEFAULT '',
            bound_ts REAL)""")
        connection.execute(
            "INSERT INTO selfmod_pr_identity "
            "(pr_url,run_id,pr_number,branch,pushed_sha,pr_id,author_id,"
            "head_repo_id,base_repo_id,head_ref,base_ref,bound_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (URL, "run-owned", 42, BRANCH, SHA, 5001, 7001,
             9001, 9001, BRANCH, "main", 1.0),
        )
    reopened = ObservabilityLedger(path)
    try:
        assert reopened.get_pr_identity(URL)["created_pr_id"] == 0
        assert reopened.bind_pr_identity("run-owned", URL, _observation()) is False
        assert reopened.validate_pr_identity("run-owned", URL, _observation()) is False
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
        return PRCreationReceipt(**_receipt(head_ref=_branch))

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
        assert identity["created_author_id"] == 7001
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


def test_authenticated_create_response_yields_structured_receipt(monkeypatch):
    import sys

    class Response:
        status_code = 201
        def json(self):
            return {
                "html_url": URL, "number": 42, "id": 5001, "user": {"id": 7001},
                "head": {"repo": {"id": 9001}, "ref": BRANCH, "sha": SHA},
                "base": {"repo": {"id": 9001}, "ref": "main"},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=Client))
    receipt = asyncio.run(github_pr_opener("token", "Degi-ceo", "HiveOS")(
        BRANCH, "candidate", "body",
    ))
    assert receipt == PRCreationReceipt(**_receipt())


def test_malformed_create_response_remains_url_only(monkeypatch):
    import sys

    class Response:
        status_code = 201
        def json(self):
            return {
                "html_url": URL, "number": 42, "id": 5001,
                "user": {"id": 7001},
                "head": {"repo": {"id": 9001}, "ref": BRANCH, "sha": [SHA]},
                "base": {"repo": {"id": 9001}, "ref": "main"},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=Client))
    assert asyncio.run(github_pr_opener("token", "Degi-ceo", "HiveOS")(
        BRANCH, "candidate", "body",
    )) == URL


def test_create_response_from_other_repo_cannot_be_a_receipt(monkeypatch):
    import sys
    other_url = "https://github.com/other/project/pull/42"

    class Response:
        status_code = 201
        def json(self):
            return {
                "html_url": other_url, "number": 42, "id": 5001,
                "user": {"id": 7001},
                "head": {"repo": {"id": 9001}, "ref": BRANCH, "sha": SHA},
                "base": {"repo": {"id": 9001}, "ref": "main"},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=Client))
    opened = asyncio.run(github_pr_opener("token", "Degi-ceo", "HiveOS")(
        BRANCH, "candidate", "body",
    ))
    assert opened == other_url
    assert not isinstance(opened, PRCreationReceipt)


def test_non_creation_success_status_remains_url_only(monkeypatch):
    import sys

    class Response:
        status_code = 200
        def json(self):
            return {
                "html_url": URL, "number": 42, "id": 5001,
                "user": {"id": 7001},
                "head": {"repo": {"id": 9001}, "ref": BRANCH, "sha": SHA},
                "base": {"repo": {"id": 9001}, "ref": "main"},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=Client))
    assert asyncio.run(github_pr_opener("token", "Degi-ceo", "HiveOS")(
        BRANCH, "candidate", "body",
    )) == URL


def test_legacy_string_opener_preserves_observation_but_never_writer_authority(tmp_path):
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
        ).propose("candidate", "", _apply_ok, approved_review=True, run_id="run-owned"))
        assert result["pr_url"] == URL
        assert "pr_creation" not in result
        identity = ledger.get_pr_identity(URL)
        assert identity is not None and identity["created_pr_id"] == 0
        assert ledger.bind_pr_identity("run-owned", URL, _observation(
            head_ref=result["branch"],
        )) is False
    finally:
        ledger.close()


def test_receipt_for_different_pushed_sha_is_not_persisted(tmp_path):
    from tests.test_self_mod import _apply_ok, _runner

    basic_run = _runner()
    async def run(command, cwd=None):
        if command == ["git", "rev-parse", "HEAD"] and cwd != str(tmp_path):
            return 0, SHA + "\n"
        return await basic_run(command, cwd)

    async def open_pr(branch, _title, _body):
        return PRCreationReceipt(**_receipt(head_ref=branch, head_sha="b" * 40))

    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    try:
        result = asyncio.run(SelfModifier(
            repo_root=str(tmp_path), run=run, open_pr=open_pr, history_store=ledger,
        ).propose("candidate", "", _apply_ok, approved_review=True, run_id="run-owned"))
        assert result["pr_url"] == URL
        assert "pr_creation" not in result
        assert ledger.get_pr_identity(URL)["created_pr_id"] == 0
    finally:
        ledger.close()


def test_feedback_rounds_are_atomic_capped_and_never_retry_uncertain(tmp_path):
    path = tmp_path / "feedback.sqlite"
    seed = ObservabilityLedger(path)
    _record(seed)
    assert seed.bind_pr_identity("run-owned", URL, _observation())
    seed.close()

    def reserve(key: str):
        ledger = ObservabilityLedger(path)
        try:
            return ledger.reserve_pr_feedback_round(
                "run-owned", URL, _observation(), feedback_key=key,
            )
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(reserve, ("ci:a", "ci:b")))
    assert sum(item is not None for item in (first, second)) == 1
    assert reserve("ci:c") is None  # No lease expiry or implicit retry.
    ledger = ObservabilityLedger(path)
    try:
        assert ledger.finish_pr_feedback_round(URL, 1, state="uncertain")
        assert ledger.finish_pr_feedback_round(URL, 1, state="pushed", new_sha="b" * 40) is False
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key="ci:d",
        ) is None
        assert [r["state"] for r in ledger.get_pr_feedback_rounds(URL)] == ["uncertain"]
    finally:
        ledger.close()


def test_feedback_round_cap_and_head_advance_require_new_exact_observation(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "feedback.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation())
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key="ci:first",
        )["round"] == 1
        assert ledger.finish_pr_feedback_round(URL, 1, state="pushed", new_sha="b" * 40)
        assert ledger.validate_pr_identity("run-owned", URL, _observation()) is False
        next_head = _observation(head_sha="b" * 40)
        assert ledger.validate_pr_identity("run-owned", URL, next_head)
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, next_head, feedback_key="ci:first",
        ) is None
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, next_head, feedback_key="ci:second",
        )["round"] == 2
        assert ledger.finish_pr_feedback_round(URL, 2, state="failed")
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, next_head, feedback_key="ci:third",
        ) is None
        assert len(ledger.get_pr_feedback_rounds(URL)) == 2
    finally:
        ledger.close()


def test_feedback_key_is_stored_only_as_opaque_digest(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "feedback.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation())
        key = "ci:known-sensitive-fragment-45981"
        reserved = ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key=key,
        )
        assert reserved is not None
        assert key not in str(reserved)
        assert key not in str(ledger.get_pr_feedback_rounds(URL))
    finally:
        ledger.close()


def test_standdown_reservation_is_once_only_across_processes(tmp_path):
    path = tmp_path / "standdown.sqlite"
    seed = ObservabilityLedger(path)
    _record(seed)
    assert seed.bind_pr_identity("run-owned", URL, _observation())
    seed.close()

    def reserve(_index: int):
        ledger = ObservabilityLedger(path)
        try:
            return ledger.reserve_pr_standdown(
                "run-owned", URL, _observation(), reason_code="round_cap",
            )
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))
    assert sum(bool(value) for value in results) == 1
    ledger = ObservabilityLedger(path)
    try:
        marker = next(value for value in results if value)
        assert ledger.get_pr_standdown(URL)["marker"] == marker
        assert ledger.mark_pr_standdown(URL, marker, state="uncertain")
        assert ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="round_cap",
        ) is None
    finally:
        ledger.close()


def test_standdown_cannot_race_an_active_feedback_round(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "standdown-race.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation())
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key="review:signal",
        )
        assert ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="review_ambiguous",
        ) is None
        assert ledger.finish_pr_feedback_round(URL, 1, state="failed")
        assert ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="review_failed",
        ) is not None
    finally:
        ledger.close()


def test_review_uncertain_standdown_reason_is_durable(tmp_path):
    path = tmp_path / "review-uncertain.sqlite"
    ledger = ObservabilityLedger(path)
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation())
        assert ledger.reserve_pr_feedback_round(
            "run-owned", URL, _observation(), feedback_key="review:signal",
        )
        assert ledger.finish_pr_feedback_round(URL, 1, state="uncertain")
        marker = ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="review_uncertain",
        )
        assert marker is not None
    finally:
        ledger.close()
    reopened = ObservabilityLedger(path)
    try:
        assert reopened.get_pr_standdown(URL)["reason_code"] == "review_uncertain"
        assert reopened.reserve_pr_standdown(
            "run-owned", URL, _observation(), reason_code="review_uncertain",
        ) is None
    finally:
        reopened.close()


def test_round_and_standdown_reservations_are_mutually_exclusive_across_connections(tmp_path):
    from threading import Barrier

    path = tmp_path / "feedback-standdown-race.sqlite"
    seed = ObservabilityLedger(path)
    _record(seed)
    assert seed.bind_pr_identity("run-owned", URL, _observation())
    seed.close()
    barrier = Barrier(2)

    def reserve(kind):
        ledger = ObservabilityLedger(path)
        try:
            barrier.wait(timeout=5)
            if kind == "round":
                return ledger.reserve_pr_feedback_round(
                    "run-owned", URL, _observation(), feedback_key="review:signal",
                )
            return ledger.reserve_pr_standdown(
                "run-owned", URL, _observation(), reason_code="review_ambiguous",
            )
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(reserve, ("round", "standdown")))
    assert sum(value is not None for value in result) == 1


def test_real_ledger_standdown_controller_reserves_and_posts_once(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "standdown-controller.sqlite")
    _record(ledger)
    assert ledger.bind_pr_identity("run-owned", URL, _observation())
    assert ledger.reserve_pr_feedback_round(
        "run-owned", URL, _observation(), feedback_key="ci:one",
    )
    assert ledger.finish_pr_feedback_round(URL, 1, state="pushed", new_sha="b" * 40)
    second = _observation(head_sha="b" * 40)
    assert ledger.reserve_pr_feedback_round(
        "run-owned", URL, second, feedback_key="ci:two",
    )
    assert ledger.finish_pr_feedback_round(URL, 2, state="pushed", new_sha="c" * 40)
    live = _observation(
        head_sha="c" * 40, status="checks_failed", ci_state="failed",
        ownership_verified=True,
        checks=[{"name": "linux-tests", "conclusion": "failure"}],
    )

    class Observer:
        async def observe(self, number):
            assert number == 42
            return PRObservation(
                number=42, url=URL, state="open", head_sha="c" * 40,
                status="checks_failed", checks_total=1, checks_failed=1,
                checks_pending=0, review_state="waiting", changes_requested=0,
                checks=({"name": "linux-tests", "status": "completed",
                         "conclusion": "failure"},),
                pr_id=5001, author_id=7001, head_repo_id=9001,
                head_ref=BRANCH, base_repo_id=9001, base_ref="main",
                ci_state="failed",
            )

    class Commenter:
        def __init__(self):
            self.posts = []

        async def post_standdown(self, number, *, marker, reason_code, failed_checks):
            self.posts.append((number, marker, reason_code, failed_checks))
            return 33

    commenter = Commenter()
    try:
        first = asyncio.run(stand_down_once(
            ledger, commenter, run_id="run-owned", pr_url=URL,
            snapshot=live, observer=Observer(),
        ))
        second_result = asyncio.run(stand_down_once(
            ledger, commenter, run_id="run-owned", pr_url=URL,
            snapshot=live, observer=Observer(),
        ))
        assert first == {"status": "posted", "comment_id": 33}
        assert second_result == {"status": "already_reserved"}
        assert len(commenter.posts) == 1
        assert commenter.posts[0][0] == 42
        assert commenter.posts[0][2:] == ("round_cap", ("linux-tests",))
        assert ledger.get_pr_standdown(URL)["state"] == "posted"
    finally:
        ledger.close()


def test_real_observer_and_ledger_can_confirm_one_ci_repair_round(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "ci-controller.sqlite")
    _record(ledger)
    assert ledger.bind_pr_identity("run-owned", URL, _observation())

    def pr(sha, status, ci_state):
        return PRObservation(
            number=42, url=URL, state="open", head_sha=sha,
            status=status, checks_total=1, checks_failed=int(ci_state == "failed"),
            checks_pending=int(ci_state == "pending"),
            review_state="waiting", changes_requested=0,
            pr_id=5001, author_id=7001, head_repo_id=9001,
            head_ref=BRANCH, base_repo_id=9001, base_ref="main",
            ci_state=ci_state,
        )

    class Observer:
        def __init__(self):
            self.calls = 0

        async def observe(self, number):
            assert number == 42
            self.calls += 1
            return pr(SHA, "checks_failed", "failed") if self.calls == 1 else pr(
                "b" * 40, "checks_pending", "pending",
            )

    class Repairer:
        def __init__(self):
            self.calls = []

        async def __call__(self, branch, expected_sha):
            self.calls.append((branch, expected_sha))
            return {"ok": True, "stage": "pushed", "head_sha": "b" * 40}

    observer, repairer = Observer(), Repairer()
    try:
        caller = _observation(
            status="checks_failed", ci_state="failed", ownership_verified=True,
        )
        result = asyncio.run(repair_failed_ci_once(
            ledger, observer, repairer, run_id="run-owned",
            pr_url=URL, snapshot=caller,
        ))
        assert result == {"status": "pushed", "round": 1, "head_sha": "b" * 40}
        assert observer.calls == 2
        assert repairer.calls == [(BRANCH, SHA)]
        assert ledger.get_pr_feedback_rounds(URL)[0]["state"] == "pushed"
        assert ledger.get_pr_identity(URL)["pushed_sha"] == "b" * 40
    finally:
        ledger.close()


def test_standdown_rejects_free_form_reason_without_reserving(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "standdown.sqlite")
    try:
        _record(ledger)
        assert ledger.bind_pr_identity("run-owned", URL, _observation())
        assert ledger.reserve_pr_standdown(
            "run-owned", URL, _observation(),
            reason_code="secretlikecontent45981",
        ) is None
        assert ledger.get_pr_standdown(URL) is None
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
