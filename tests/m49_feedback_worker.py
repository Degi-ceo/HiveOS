"""Separate-process half of the deterministic CI/review feedback integration test."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from hive.core.config import HiveConfig
from hive.core.pr_review import ReviewSelection, ReviewSuggestion
from hive.core.self_mod import _default_run
from hive.observability.supervised_review_writer import SupervisedReviewWriter
from hive.runtime import HiveOS
from tests.test_m41_supervised_review_prepare import IMAGE


async def _publish(repo: Path, branch: str, head: str, feedback_kind: str = "ci") -> dict:
    if feedback_kind not in {"ci", "review"}:
        raise ValueError("unsupported feedback kind")
    pr_url = "https://github.com/owner/repo/pull/42"
    snapshot = {
        "number": 42, "url": pr_url, "state": "open",
        "status": "draft" if feedback_kind == "ci" else "changes_requested",
        "draft": feedback_kind == "ci",
        "ci_state": "failed" if feedback_kind == "ci" else "passed",
        "ownership_verified": True,
        "head_sha": head, "pr_id": 99, "author_id": 4,
        "head_repo_id": 8, "base_repo_id": 8,
        "head_ref": branch, "base_ref": "main",
    }

    class Router:
        async def complete(self, _messages, **_kwargs):
            raise AssertionError("publication must not invoke a model")

        async def aclose(self):
            pass

    class Observer:
        available = True

        async def observe(self, number):
            assert number == 42
            return SimpleNamespace(as_dict=lambda: dict(snapshot))

    push_attempts = 0

    async def local_git_with_trusted_origin(command, cwd):
        nonlocal push_attempts
        if command[:3] == ["git", "remote", "get-url"]:
            return 0, "https://github.com/owner/repo.git\n"
        if "push" in command:
            push_attempts += 1
        return await _default_run(command, cwd)

    config = replace(
        HiveConfig.from_env(root=repo, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    hive = HiveOS.build(config, router=Router())
    try:
        hive.config = replace(
            hive.config, pr_feedback_enabled=True, github_owner="owner",
            github_repo="repo", github_token="test-token",
            pr_reviewer_ids=frozenset({"1234"}) if feedback_kind == "review" else frozenset(),
        )
        hive.pr_observer = Observer()
        if feedback_kind == "review":
            suggestion = ReviewSuggestion(
                thread_id="thread-1", comment_id="comment-1", reviewer_id=1234,
                path="src/hive/example.py", line=1, replacement="value = 2",
                head_sha=head, signal_digest="d" * 64,
            )

            class Reader:
                async def select(self, number, sha, reviewers):
                    assert (number, sha, reviewers) == (42, head, frozenset({1234}))
                    return ReviewSelection("ready", suggestion)

            hive.pr_review_reader = Reader()
            hive.pr_commenter = object()
        hive.supervised_review_writer = SupervisedReviewWriter(
            repo_root=repo, state_db=config.state_db,
            pinned_image_digest=IMAGE, git_run=local_git_with_trusted_origin,
        )
        pending = hive.pr_review_authorizations.public_pending()
        assert len(pending) == 1
        before = await hive.react_to_failed_pr_ci(snapshot)
        assert before["status"] == "review_pending" and before["resumed"] is True
        assert hive.pr_review_authorizations.decide(
            pending[0]["id"], pending[0]["binding_digest"],
            approved=True, principal="human:approver",
        )
        result = await hive.react_to_failed_pr_ci(snapshot)
        return {**result, "push_attempts": push_attempts}
    finally:
        await hive.aclose()


if __name__ == "__main__":
    print("M49_RESULT=" + json.dumps(asyncio.run(_publish(
        Path(sys.argv[1]), sys.argv[2], sys.argv[3],
        sys.argv[4] if len(sys.argv) > 4 else "ci",
    ))))
