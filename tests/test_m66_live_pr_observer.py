"""Opt-in read-only proof against the GitHub PR that triggered this CI run."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from hive.core.pr_observer import GitHubPRObserver


_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_PENDING = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    response = await client.get(f"https://api.github.com{path}")
    response.raise_for_status()
    result = response.json()
    assert isinstance(result, dict)
    return result


async def _get_list(client: httpx.AsyncClient, path: str) -> list:
    response = await client.get(f"https://api.github.com{path}")
    response.raise_for_status()
    result = response.json()
    assert isinstance(result, list)
    return result


async def _raw_head_evidence(
    client: httpx.AsyncClient, owner: str, repo: str, number: int, expected_sha: str,
) -> tuple[dict, dict, tuple, bool] | None:
    root = f"/repos/{owner}/{repo}"
    pr = await _get(client, f"{root}/pulls/{number}")
    if pr.get("state") != "open" or pr.get("head", {}).get("sha") != expected_sha:
        return None
    checks = await _get(client, f"{root}/commits/{expected_sha}/check-runs?per_page=100&filter=latest")
    status = await _get(client, f"{root}/commits/{expected_sha}/status")
    collections = (
        await _get_list(client, f"{root}/pulls/{number}/reviews?per_page=100"),
        await _get_list(client, f"{root}/pulls/{number}/comments?per_page=100"),
        await _get_list(client, f"{root}/issues/{number}/comments?per_page=100"),
    )
    rows = checks.get("check_runs")
    statuses = status.get("statuses")
    assert isinstance(rows, list) and isinstance(statuses, list)
    assert isinstance(checks.get("total_count"), int)
    assert isinstance(status.get("total_count"), int)
    assert status.get("sha") == expected_sha
    assert all(row.get("head_sha") == expected_sha for row in rows)
    signature = (
        checks["total_count"],
        tuple(sorted((str(row.get("id")), str(row.get("status")),
                      str(row.get("conclusion"))) for row in rows)),
        tuple(sorted((str(row.get("context")), str(row.get("state"))) for row in statuses)),
        status.get("state"),
        tuple(len(rows) for rows in collections),
    )
    complete = (checks["total_count"] == len(rows)
                and status["total_count"] == len(statuses)
                and all(len(page) < 100 for page in collections))
    return checks, status, signature, complete


@pytest.mark.skipif(
    not os.getenv("HIVE_TEST_LIVE_PR_NUMBER"),
    reason="requires a same-repository pull_request event and read-only token",
)
def test_observer_matches_live_pull_request_event():
    token = os.environ["HIVE_TEST_LIVE_PR_TOKEN"]
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    number = int(os.environ["HIVE_TEST_LIVE_PR_NUMBER"])
    expected_sha = os.environ["HIVE_TEST_LIVE_HEAD_SHA"]

    async def compare() -> tuple[dict, dict, dict, bool]:
        observer = GitHubPRObserver(token, owner, repo)
        deadline = time.monotonic() + 90
        attempt = 0
        last_cause = "no current-head check runs"
        async with httpx.AsyncClient(
            timeout=20,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        ) as client:
            while time.monotonic() < deadline:
                before = await _raw_head_evidence(client, owner, repo, number, expected_sha)
                if before is None:
                    pytest.fail("the PR event was superseded or closed before observation")
                observed = (await observer.observe(number)).as_dict()
                after = await _raw_head_evidence(client, owner, repo, number, expected_sha)
                if after is None:
                    pytest.fail("the PR event was superseded or closed during observation")
                if before[2] == after[2] and before[0]["total_count"] >= 1:
                    return observed, before[0], before[1], before[3]
                last_cause = (
                    "no current-head check runs" if before[0]["total_count"] < 1
                    else "check evidence changed during observation"
                )
                attempt += 1
                await asyncio.sleep(min(2 ** min(attempt, 4), max(0, deadline - time.monotonic())))
        pytest.fail(f"current-head evidence unavailable after bounded observation: {last_cause}")

    try:
        observed, checks, status, collections_complete = asyncio.run(
            asyncio.wait_for(compare(), timeout=90)
        )
    except TimeoutError:
        pytest.fail("current-head evidence did not stabilize within 90 seconds")
    check_rows = checks["check_runs"]
    status_rows = status["statuses"]
    failed = sum(str(row.get("conclusion") or "").casefold() in _FAILED for row in check_rows)
    pending = sum(str(row.get("status") or "").casefold() in _PENDING for row in check_rows)
    classic_states = {str(row.get("state") or "").casefold() for row in status_rows}
    expected_ci = (
        "incomplete" if not collections_complete else
        "pending" if pending or "pending" in classic_states else
        "failed" if failed or classic_states & {"failure", "error"} else "passed"
    )

    assert observed["number"] == number
    assert observed["url"] == f"https://github.com/{owner}/{repo}/pull/{number}"
    assert observed["state"] == "open"
    assert observed["head_sha"] == expected_sha
    assert observed["head_ref"] == os.environ["HIVE_TEST_LIVE_HEAD_REF"]
    assert observed["base_ref"] == os.environ["HIVE_TEST_LIVE_BASE_REF"]
    assert observed["pr_id"] == int(os.environ["HIVE_TEST_LIVE_PR_ID"])
    assert observed["author_id"] == int(os.environ["HIVE_TEST_LIVE_AUTHOR_ID"])
    assert observed["head_repo_id"] == observed["base_repo_id"] == int(
        os.environ["HIVE_TEST_LIVE_REPO_ID"]
    )
    assert observed["checks_total"] == checks["total_count"]
    assert observed["checks_failed"] == failed
    assert observed["checks_pending"] == pending
    assert observed["ci_state"] == expected_ci
