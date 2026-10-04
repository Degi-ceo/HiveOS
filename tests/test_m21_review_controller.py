"""Durable review feedback controller remains one-shot and identity-bound."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

from hive.core.pr_feedback import GitHubPRCommenter, apply_review_once, stand_down_review_once
from hive.core.pr_review import ReviewSelection, ReviewSuggestion


OLD = "a" * 40
NEW = "b" * 40
URL = "https://github.com/owner/repo/pull/42"
BRANCH = "hive/auto-" + "c" * 32
MARKER = "c4ef9136-5e56-4dc1-8b77-3537de141de0"
SIGNAL = ReviewSuggestion(
    thread_id="PRRT_1", comment_id="PRRC_1", reviewer_id=1234,
    path="docs/guide.md", line=4, replacement="Correct sentence.",
    head_sha=OLD, signal_digest="d" * 64,
)


def _snapshot(sha=OLD):
    return {
        "number": 42, "url": URL, "state": "open", "status": "changes_requested",
        "ci_state": "passed", "ownership_verified": True, "head_sha": sha,
        "pr_id": 99, "author_id": 4, "head_repo_id": 8, "base_repo_id": 8,
        "head_ref": BRANCH, "base_ref": "main",
    }


class Ledger:
    def __init__(self):
        self.head = OLD
        self.rounds = []
        self.standdown = None
        self.reservations = 0
        self.expected_reason = "review_ambiguous"

    def validate_pr_identity(self, run_id, pr_url, snapshot):
        return (run_id == "run-42" and pr_url == URL
                and snapshot.get("url") == URL and snapshot.get("head_sha") == self.head
                and snapshot.get("pr_id") == 99 and snapshot.get("head_ref") == BRANCH)

    def get_pr_identity(self, pr_url):
        assert pr_url == URL
        return {
            "bound": True, "pr_number": 42, "pr_id": 99, "author_id": 4,
            "head_repo_id": 8, "base_repo_id": 8, "branch": BRANCH,
            "base_ref": "main", "pushed_sha": self.head,
        }

    def get_pr_feedback_rounds(self, pr_url):
        assert pr_url == URL
        return [dict(row) for row in self.rounds]

    def get_pr_standdown(self, pr_url):
        assert pr_url == URL
        return self.standdown

    def reserve_pr_feedback_round(self, run_id, pr_url, live, *, feedback_key):
        assert self.validate_pr_identity(run_id, pr_url, live)
        assert feedback_key == f"review:{SIGNAL.signal_digest}"
        if self.rounds:
            return None
        self.reservations += 1
        self.rounds.append({
            "round": 1, "state": "reserved", "expected_sha": OLD,
            "feedback_key": hashlib.sha256(feedback_key.encode()).hexdigest(),
        })
        return {"round": 1, "expected_sha": OLD}

    def finish_pr_feedback_round(self, pr_url, round_number, *, state, new_sha=""):
        assert (pr_url, round_number) == (URL, 1)
        self.rounds[0]["state"] = state
        if state == "pushed":
            self.head = new_sha
        return True

    def reserve_pr_standdown(self, run_id, pr_url, live, *, reason_code):
        assert self.validate_pr_identity(run_id, pr_url, live)
        assert reason_code == self.expected_reason
        if self.standdown is not None:
            return None
        self.standdown = {"marker": MARKER, "state": "reserved"}
        return MARKER

    def mark_pr_standdown(self, pr_url, marker, *, state):
        assert (pr_url, marker) == (URL, MARKER)
        self.standdown["state"] = state
        return True


class Observer:
    def __init__(self):
        self.sha = OLD
        self.calls = 0

    async def observe(self, number):
        assert number == 42
        self.calls += 1
        return SimpleNamespace(as_dict=lambda: _snapshot(self.sha))


class Reader:
    def __init__(self, selection=None):
        self.selection = selection or ReviewSelection("ready", SIGNAL)
        self.calls = 0

    async def select(self, number, sha, reviewers):
        assert number == 42 and sha == OLD and reviewers == frozenset({1234})
        self.calls += 1
        return self.selection


def _run(ledger, observer, reader, repairer):
    return asyncio.run(apply_review_once(
        ledger, observer, reader, repairer, run_id="run-42", pr_url=URL,
        snapshot=_snapshot(), reviewer_ids=frozenset({1234}),
    ))


def test_review_round_reserves_once_and_confirms_exact_new_head():
    ledger, observer, reader = Ledger(), Observer(), Reader()
    calls = []

    async def repair(branch, sha, suggestion):
        calls.append((branch, sha, suggestion))
        observer.sha = NEW
        return {"ok": True, "stage": "pushed", "head_sha": NEW}

    result = _run(ledger, observer, reader, repair)
    assert result == {"status": "pushed", "round": 1, "head_sha": NEW}
    assert ledger.reservations == 1 and ledger.head == NEW
    assert calls == [(BRANCH, OLD, SIGNAL)]
    assert observer.calls == 2 and reader.calls == 1
    assert _run(ledger, observer, reader, repair) == {"status": "wait"}


def test_ambiguous_or_stale_review_never_reserves_or_writes():
    async def repair(_branch, _sha, _suggestion):
        raise AssertionError("repair must not execute")

    ledger, observer, reader = Ledger(), Observer(), Reader(ReviewSelection("ambiguous"))
    assert _run(ledger, observer, reader, repair) == {"status": "review_ambiguous"}
    assert ledger.reservations == 0
    observer.sha = NEW
    assert _run(ledger, observer, reader, repair) == {"status": "wait"}
    assert ledger.reservations == 0


def test_pr_author_cannot_act_as_out_of_band_reviewer():
    ledger, observer = Ledger(), Observer()
    reader = Reader(ReviewSelection("ready", replace(SIGNAL, reviewer_id=4)))

    async def repair(_branch, _sha, _suggestion):
        raise AssertionError("self-review must not authorize a candidate")

    assert _run(ledger, observer, reader, repair) == {"status": "wait"}
    assert ledger.reservations == 0


def test_failed_candidate_spends_round_without_retry():
    ledger, observer, reader = Ledger(), Observer(), Reader()

    async def repair(_branch, _sha, _suggestion):
        return {"ok": False, "stage": "test"}

    assert _run(ledger, observer, reader, repair) == {"status": "failed"}
    assert ledger.rounds[0]["state"] == "failed"
    assert _run(ledger, observer, reader, repair) == {"status": "wait"}
    assert ledger.reservations == 1


def test_review_ambiguity_posts_one_fixed_standdown_after_fresh_checks():
    ledger, observer = Ledger(), Observer()
    reader = Reader(ReviewSelection("ambiguous"))
    posted = []

    class Commenter:
        async def post_standdown(self, number, *, marker, reason_code):
            posted.append((number, marker, reason_code))
            return 500

    async def once():
        return await stand_down_review_once(
            ledger, observer, reader, Commenter(), run_id="run-42", pr_url=URL,
            snapshot=_snapshot(), reviewer_ids=frozenset({1234}),
            reason="review_ambiguous",
        )

    assert asyncio.run(once()) == {"status": "posted", "comment_id": 500}
    assert posted == [(42, MARKER, "review_ambiguous")]
    assert asyncio.run(once()) == {"status": "already_reserved"}
    assert len(posted) == 1


def test_uncertain_review_posts_review_specific_proposal_after_fresh_checks():
    ledger, observer, reader = Ledger(), Observer(), Reader()
    ledger.expected_reason = "review_uncertain"
    ledger.rounds = [{
        "round": 1, "state": "uncertain",
        "feedback_key": hashlib.sha256(
            f"review:{SIGNAL.signal_digest}".encode("utf-8")
        ).hexdigest(),
    }]
    posted = []

    async def poster(number, body):
        posted.append((number, body))
        return 501

    commenter = GitHubPRCommenter("private-test-token", "owner", "repo", poster=poster)

    async def once():
        return await stand_down_review_once(
            ledger, observer, reader, commenter, run_id="run-42", pr_url=URL,
            snapshot=_snapshot(), reviewer_ids=frozenset({1234}),
            reason="review_uncertain",
        )

    assert asyncio.run(once()) == {"status": "posted", "comment_id": 501}
    assert len(posted) == 1 and posted[0][0] == 42
    body = posted[0][1]
    assert "Review scope: an eligible suggestion" in body
    assert "Proposal: verify the remote PR head" in body
    assert "Failing CI checks:" not in body
    assert "private-test-token" not in body
    assert asyncio.run(once()) == {"status": "already_reserved"}
    assert len(posted) == 1
