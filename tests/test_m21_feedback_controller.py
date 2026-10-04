"""One public stand-down attempt after authenticated, exhausted CI feedback."""

import asyncio

import pytest

from hive.core.pr_feedback import stand_down_once


URL = "https://github.com/owner/repo/pull/42"
MARKER = "c4ef9136-5e56-4dc1-8b77-3537de141de0"


def _snapshot(**overrides):
    value = {
        "number": 42, "url": URL, "state": "open", "status": "checks_failed",
        "ci_state": "failed", "ownership_verified": True, "head_sha": "a" * 40,
        "checks": [{"name": "linux-tests", "conclusion": "failure"}],
    }
    value.update(overrides)
    return value


class Ledger:
    def __init__(self, *, authenticated=True, rounds=2, states=None):
        self.authenticated = authenticated
        self.rounds = rounds
        self.states = states or ("pushed",) * rounds
        self.marker_available = True
        self.marks = []

    def validate_pr_identity(self, run_id, pr_url, observation):
        assert (run_id, pr_url, observation["number"]) == ("run-42", URL, 42)
        return self.authenticated

    def get_pr_feedback_rounds(self, pr_url):
        assert pr_url == URL
        return [{"round": index + 1, "state": self.states[index]}
                for index in range(self.rounds)]

    def reserve_pr_standdown(self, run_id, pr_url, observation, *, reason_code):
        assert (run_id, pr_url, observation["number"]) == ("run-42", URL, 42)
        assert reason_code in {"round_cap", "repair_failed", "feedback_ambiguous"}
        if not self.marker_available:
            return None
        self.marker_available = False
        return MARKER

    def mark_pr_standdown(self, pr_url, marker, *, state):
        self.marks.append((pr_url, marker, state))
        return True


class Commenter:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def post_standdown(self, number, *, marker, reason_code, failed_checks=()):
        self.calls.append((number, marker, reason_code, failed_checks))
        if self.error is not None:
            raise self.error
        return 912


class Observer:
    def __init__(self, first=None, second=None):
        self.first = first or _snapshot()
        self.second = second or self.first
        self.calls = 0

    async def observe(self, number):
        assert number == 42
        self.calls += 1
        return self.first if self.calls == 1 else self.second


def test_exhausted_authored_pr_posts_once_and_marks_success():
    ledger, commenter = Ledger(), Commenter()
    observer = Observer()
    first = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    second = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert first == {"status": "posted", "comment_id": 912}
    assert second == {"status": "already_reserved"}
    assert commenter.calls == [(42, MARKER, "round_cap", ("linux-tests",))]
    assert ledger.marks == [(URL, MARKER, "posted")]


def test_unowned_or_unfinished_ci_never_posts():
    for ledger, snapshot in (
        (Ledger(authenticated=False), _snapshot()),
        (Ledger(rounds=1), _snapshot()),
        (Ledger(), _snapshot(ci_state="pending", status="checks_pending")),
        (Ledger(), _snapshot(ownership_verified=False)),
    ):
        commenter = Commenter()
        result = asyncio.run(stand_down_once(
            ledger, commenter, run_id="run-42", pr_url=URL, snapshot=snapshot,
            observer=Observer(snapshot),
        ))
        assert result == {"status": "wait"}
        assert commenter.calls == []


def test_ambiguous_post_is_marked_uncertain_and_not_retried():
    ledger, commenter = Ledger(), Commenter(error=RuntimeError("network uncertain"))
    observer = Observer()
    first = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    second = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert first == {"status": "uncertain"}
    assert second == {"status": "already_reserved"}
    assert len(commenter.calls) == 1
    assert ledger.marks == [(URL, MARKER, "uncertain")]


def test_storage_error_after_ambiguous_post_does_not_repeat_or_leak():
    class BrokenMarkLedger(Ledger):
        def mark_pr_standdown(self, _pr_url, _marker, *, state):
            raise RuntimeError("credential-bearing storage failure")

    ledger = BrokenMarkLedger()
    commenter = Commenter(error=RuntimeError("credential-bearing network failure"))
    observer = Observer()
    first = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    second = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert first == {"status": "uncertain"}
    assert second == {"status": "already_reserved"}
    assert len(commenter.calls) == 1


def test_cancelled_public_post_keeps_one_shot_reservation():
    ledger = Ledger()
    commenter = Commenter(error=asyncio.CancelledError())
    observer = Observer()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stand_down_once(
            ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
            observer=observer,
        ))
    second = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert second == {"status": "already_reserved"}
    assert len(commenter.calls) == 1
    assert ledger.marks == [(URL, MARKER, "uncertain")]


def test_closed_pr_between_reservation_and_post_never_gets_public_comment():
    ledger, commenter = Ledger(), Commenter()
    observer = Observer(second=_snapshot(state="closed", status="closed"))
    result = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert result == {"status": "uncertain"}
    assert observer.calls == 2
    assert commenter.calls == []
    assert ledger.marks == [(URL, MARKER, "uncertain")]


def test_stale_caller_snapshot_cannot_reserve_a_comment():
    ledger, commenter = Ledger(), Commenter()
    observer = Observer(first=_snapshot(head_sha="b" * 40))
    result = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=observer,
    ))
    assert result == {"status": "wait"}
    assert ledger.marker_available is True
    assert commenter.calls == []


@pytest.mark.parametrize("state,reason", [
    ("failed", "repair_failed"), ("uncertain", "feedback_ambiguous"),
])
def test_terminal_round_posts_safe_single_standdown(state, reason):
    ledger, commenter = Ledger(rounds=1, states=(state,)), Commenter()
    result = asyncio.run(stand_down_once(
        ledger, commenter, run_id="run-42", pr_url=URL, snapshot=_snapshot(),
        observer=Observer(),
    ))
    assert result == {"status": "posted", "comment_id": 912}
    assert commenter.calls == [(42, MARKER, reason, ("linux-tests",))]
    assert ledger.marks == [(URL, MARKER, "posted")]
