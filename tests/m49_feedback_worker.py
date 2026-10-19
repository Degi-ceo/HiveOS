"""Separate-process half of the deterministic PR feedback integration test."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from hive.core.config import HiveConfig
from hive.core.self_mod import _default_run
from hive.observability.supervised_review_writer import SupervisedReviewWriter
from hive.runtime import HiveOS
from tests.test_m41_supervised_review_prepare import IMAGE


async def _publish(repo: Path, branch: str, head: str) -> dict:
    pr_url = "https://github.com/owner/repo/pull/42"
    snapshot = {
        "number": 42, "url": pr_url, "state": "open", "status": "draft",
        "draft": True, "ci_state": "failed", "ownership_verified": True,
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
        )
        hive.pr_observer = Observer()
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
    ))))
