"""Only conclusive CI on an authenticated Hive PR can start write-back."""

import asyncio

import httpx
import pytest

from hive.core.pr_feedback import GitHubPRCommenter, feedback_action


def _snapshot(**overrides):
    value = {
        "state": "open", "status": "checks_failed", "ci_state": "failed",
        "ownership_verified": True, "number": 42,
    }
    value.update(overrides)
    return value


def test_current_failed_ci_on_authenticated_creation_is_repairable():
    assert feedback_action(_snapshot(), authenticated_creation=True, rounds_used=0) == "repair"


def test_draft_with_complete_failed_ci_is_repairable_but_pending_draft_is_not():
    draft = _snapshot(status="draft", draft=True)
    assert feedback_action(draft, authenticated_creation=True, rounds_used=0) == "repair"
    assert feedback_action(draft, authenticated_creation=True, rounds_used=2) == "stand_down"
    assert feedback_action(
        _snapshot(status="draft", draft=True, ci_state="pending"),
        authenticated_creation=True, rounds_used=0,
    ) == "wait"
    assert feedback_action(
        _snapshot(status="draft", draft=True, ci_state="incomplete"),
        authenticated_creation=True, rounds_used=0,
    ) == "wait"


@pytest.mark.parametrize("snapshot,authenticated", (
    (_snapshot(), False),
    (_snapshot(ownership_verified=False), True),
    (_snapshot(state="closed"), True),
    (_snapshot(status="draft"), True),
    (_snapshot(status="checks_failed", draft=True), True),
    (_snapshot(status="checks_pending", ci_state="pending"), True),
    (_snapshot(status="incomplete_evidence", ci_state="incomplete"), True),
    (_snapshot(status="ready_for_human_merge", ci_state="passed"), True),
    (_snapshot(status="changes_requested", ci_state="unknown"), True),
    (_snapshot(number=0), True),
))
def test_unverified_or_nonfinal_evidence_never_authorizes_write(snapshot, authenticated):
    assert feedback_action(
        snapshot, authenticated_creation=authenticated, rounds_used=0,
    ) == "wait"


def test_exhausted_failed_ci_stands_down_without_another_repair():
    assert feedback_action(_snapshot(), authenticated_creation=True, rounds_used=2) == (
        "stand_down"
    )


def test_round_count_is_fail_closed_for_invalid_values():
    for rounds in (-1, None, "1", 2.5):
        assert feedback_action(
            _snapshot(), authenticated_creation=True, rounds_used=rounds,
        ) == "wait"


def test_standdown_comment_is_deterministic_and_contains_no_feedback_text():
    seen = []

    async def poster(number, body):
        seen.append((number, body))
        return 751

    commenter = GitHubPRCommenter(
        "private-test-token", "Degi-ceo", "HiveOS", poster=poster,
    )
    marker = "c4ef9136-5e56-4dc1-8b77-3537de141de0"
    result = asyncio.run(commenter.post_standdown(
        42, marker=marker, reason_code="round_cap",
        failed_checks=("linux-tests", "private-test-token", "bad:check"),
    ))
    assert result == 751
    assert len(seen) == 1 and seen[0][0] == 42
    assert marker in seen[0][1]
    assert "CI repair limit reached" in seen[0][1]
    assert "Failing CI checks: linux-tests." in seen[0][1]
    assert "private-test-token" not in seen[0][1]
    assert "traceback" not in seen[0][1].lower()


@pytest.mark.parametrize("number,marker,reason", (
    (0, "c4ef9136-5e56-4dc1-8b77-3537de141de0", "round_cap"),
    (42, "bad-marker", "round_cap"),
    (42, "c4ef9136-5e56-4dc1-8b77-3537de141de0", "arbitrary text"),
))
def test_standdown_comment_rejects_unbounded_inputs(number, marker, reason):
    async def poster(_number, _body):
        pytest.fail("invalid request reached poster")

    commenter = GitHubPRCommenter("token", "owner", "repo", poster=poster)
    with pytest.raises(ValueError):
        asyncio.run(commenter.post_standdown(
            number, marker=marker, reason_code=reason,
        ))


def test_standdown_transport_uses_only_fixed_issue_comment_endpoint(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"id": 912})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    commenter = GitHubPRCommenter("private-token", "owner", "repo")
    comment_id = asyncio.run(commenter.post_standdown(
        42, marker="c4ef9136-5e56-4dc1-8b77-3537de141de0",
        reason_code="round_cap",
    ))
    assert comment_id == 912
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == (
        "https://api.github.com/repos/owner/repo/issues/42/comments"
    )
    assert b"private-token" not in requests[0].content


def test_standdown_transport_does_not_echo_secret_bearing_http_errors(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            422, text="upstream says private-token leaked",
        )), **kwargs,
    ))
    commenter = GitHubPRCommenter("private-token", "owner", "repo")
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(commenter.post_standdown(
            42, marker="c4ef9136-5e56-4dc1-8b77-3537de141de0",
            reason_code="round_cap",
        ))
    assert "private-token" not in str(caught.value)


@pytest.mark.parametrize("owner,repo", (
    ("..", "repo"), ("owner", ".."), ("owner/name", "repo"),
))
def test_standdown_transport_rejects_invalid_repository_path(owner, repo):
    with pytest.raises(ValueError):
        GitHubPRCommenter("token", owner, repo)
