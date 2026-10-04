"""Issue reading stays bounded, read-only, and outside the audit payload."""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from hive.autonomy.heartbeat import Heartbeat
from hive.autonomy.tasks import DEAD, MAX_RATE_LIMIT_DEFERRALS, PENDING, TaskBoard
from hive.agents.orchestrator import ConversationOrchestrator
from hive.core.config import HiveConfig
from hive.core.types import ContentTrust, ToolResult
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.builtins import GitHubGetIssue, GitHubListIssues, WebGet, register_builtins
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.registry import ToolRegistry


def _issue(number: int, *, body: str = "", comments: int = 0) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": body,
        "labels": [{"name": "ready"}],
        "html_url": f"https://github.com/acme/hive/issues/{number}",
        "updated_at": "2026-10-04T00:00:00Z",
        "comments": comments,
    }


def test_issue_tools_register_only_with_github_token_and_remain_read_only():
    class WithoutToken(ToolRegistry):
        pass

    no_token = register_builtins(WithoutToken)
    assert "github_list_issues" not in no_token
    assert "github_get_issue" not in no_token

    class WithToken(ToolRegistry):
        pass

    tools = register_builtins(
        WithToken, github_token="test-token", github_owner="acme", github_repo="hive",
    )
    for name in ("github_list_issues", "github_get_issue"):
        assert name in tools
        assert tools[name].spec.dangerous is False
        assert tools[name].spec.category == "github"


def test_list_filters_prs_bounds_results_and_marks_remote_data_untrusted(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    observed = []
    issue = _issue(8)
    issue["title"] = "x" * 500
    pull = _issue(9)
    pull["pull_request"] = {"url": "https://api.github.com/repos/acme/hive/pulls/9"}

    async def fake_get(path, params=None):
        observed.append((path, params))
        return [issue, pull]

    monkeypatch.setattr(tool, "_get", fake_get)
    result = asyncio.run(tool.execute(state="open", labels=["ready", "P1"], limit=2, page=3))
    assert result.success
    assert result.envelope.trust is ContentTrust.UNTRUSTED
    payload = json.loads(result.content)
    assert [item["number"] for item in payload["issues"]] == [8]
    assert len(payload["issues"][0]["title"]) <= 200
    assert payload["next_page"] == 4
    assert observed == [(
        "/repos/acme/hive/issues",
        {"state": "open", "labels": "ready,P1", "per_page": 2, "page": 3},
    )]


def test_get_includes_bounded_body_labels_and_comment_page(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")
    observed = []
    issue = _issue(17, body="u" * 7000, comments=3)

    async def fake_get(path, params=None):
        observed.append((path, params))
        if path.endswith("/comments"):
            return [{"id": 5, "body": "v" * 3000, "user": {"login": "reporter"}}]
        return issue

    monkeypatch.setattr(tool, "_get", fake_get)
    result = asyncio.run(tool.execute(number=17, comment_limit=1, comment_page=2))
    assert result.success
    assert result.envelope.trust is ContentTrust.UNTRUSTED
    payload = json.loads(result.content)
    assert payload["number"] == 17
    assert payload["labels"] == ["ready"]
    assert len(payload["body"]) <= 6000
    assert payload["body_truncated"] is True
    assert payload["next_body_offset"] == 6000
    assert len(payload["comments"]) == 1
    assert len(payload["comments"][0]["body"]) <= 2000
    assert payload["comments_truncated"] is True
    assert observed == [
        ("/repos/acme/hive/issues/17", None),
        ("/repos/acme/hive/issues/17/comments", {"per_page": 1, "page": 2}),
    ]


def test_invalid_filters_and_numbers_fail_before_network(monkeypatch):
    list_tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    get_tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")

    async def forbidden_get(*_args, **_kwargs):
        raise AssertionError("network must not run for invalid input")

    monkeypatch.setattr(list_tool, "_get", forbidden_get)
    monkeypatch.setattr(get_tool, "_get", forbidden_get)
    for kwargs in (
        {"state": "invalid"}, {"limit": 0}, {"limit": 1000},
        {"labels": ["ready", 17]}, {"labels": ["ready,other"]}, {"page": 0},
    ):
        assert not asyncio.run(list_tool.execute(**kwargs)).success
    for number in (0, -1, True, "1; echo unsafe"):
        assert not asyncio.run(get_tool.execute(number=number)).success
    for kwargs in ({"body_offset": -1}, {"comment_body_offset": True},
                   {"comment_body_offset": 1_000_001}):
        assert not asyncio.run(get_tool.execute(number=17, **kwargs)).success


def test_rate_limit_and_http_error_do_not_leak_remote_text_or_crash(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    request = httpx.Request("GET", "https://api.github.com/repos/acme/hive/issues")
    response = httpx.Response(403, request=request, text="secret error body")

    async def failed_get(*_args, **_kwargs):
        raise httpx.HTTPStatusError("secret error body", request=request, response=response)

    monkeypatch.setattr(tool, "_get", failed_get)
    result = asyncio.run(tool.execute())
    assert not result.success
    assert "secret error body" not in result.content
    assert "forbidden" in result.content.lower()
    assert result.metadata.get("error_code") == "forbidden"
    assert "retry_after_seconds" not in result.metadata


def test_rate_limit_carries_bounded_retry_delay_without_remote_text(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    request = httpx.Request("GET", "https://api.github.com/repos/acme/hive/issues")
    response = httpx.Response(
        429, request=request, headers={"Retry-After": "30"}, text="private response body",
    )

    async def failed_get(*_args, **_kwargs):
        raise httpx.HTTPStatusError("private response body", request=request, response=response)

    monkeypatch.setattr(tool, "_get", failed_get)
    result = asyncio.run(tool.execute())
    assert not result.success
    assert result.metadata == {"error_code": "rate_limited", "retry_after_seconds": 30}
    assert "private response body" not in result.content


def test_primary_rate_limit_reset_sets_cooldown(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    request = httpx.Request("GET", "https://api.github.com/repos/acme/hive/issues")
    response = httpx.Response(
        403, request=request,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"},
        text="private response body",
    )

    async def limited(*_args, **_kwargs):
        raise httpx.HTTPStatusError("private response body", request=request,
                                    response=response)

    monkeypatch.setattr("hive.tools.builtins.time.time", lambda: 1000)
    monkeypatch.setattr(tool, "_get", limited)
    result = asyncio.run(tool.execute())
    assert not result.success
    assert result.metadata == {"error_code": "rate_limited", "retry_after_seconds": 300}
    assert "private response body" not in result.content


def test_rate_limit_deferral_is_durable_bounded_and_preserves_failure_budget(tmp_path):
    clock = [1000.0]
    path = tmp_path / "state.sqlite"
    board = TaskBoard(path, clock=lambda: clock[0])
    task_id = board.enqueue("tool", {"tool": "github_list_issues"}, max_attempts=2)
    for count in range(1, MAX_RATE_LIMIT_DEFERRALS + 1):
        attempt = board.claim_attempt(task_id)
        assert attempt == count
        assert board.defer_rate_limited(task_id, expected_attempt=attempt,
                                        retry_after_seconds=100)
        record = board.get(task_id)
        assert record.attempts == count
        assert record.rate_limit_count == count
        assert record.max_attempts == 2
        assert record.state == (DEAD if count == MAX_RATE_LIMIT_DEFERRALS else PENDING)
        assert board.due(now=clock[0] + 99) == []
        board.close()
        board = TaskBoard(path, clock=lambda: clock[0])
        clock[0] += 100
    assert board.claim_attempt(task_id) is None


def test_rate_limit_claim_does_not_reduce_later_failure_budget(tmp_path):
    clock = [1000.0]
    board = TaskBoard(tmp_path / "state.sqlite", clock=lambda: clock[0])
    task_id = board.enqueue("tool", {}, max_attempts=1)
    assert board.claim_attempt(task_id) == 1
    assert board.defer_rate_limited(task_id, expected_attempt=1, retry_after_seconds=10)
    clock[0] += 10
    assert board.claim_attempt(task_id) == 2
    assert board.retry_or_dead(task_id, "failure", expected_attempt=2)
    assert board.get(task_id).state == DEAD


def test_orchestrator_receives_only_safe_issue_error_code(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    request = httpx.Request("GET", "https://api.github.com/repos/acme/hive/issues")
    response = httpx.Response(403, request=request, text="private response body")

    async def forbidden(*_args, **_kwargs):
        raise httpx.HTTPStatusError("private response body", request=request,
                                    response=response)

    monkeypatch.setattr(tool, "_get", forbidden)
    executor = ToolExecutor({tool.spec.name: tool})
    agent = ConversationOrchestrator(router=object(), tool_executor=executor)
    message, result, status = asyncio.run(agent._dispatch("github_list_issues", {}))
    assert status == DispatchStatus.ERROR.value
    assert result is None
    assert message == "[tool error: github_list_issues forbidden]"
    assert "private response body" not in message


def test_existing_web_get_failure_remains_diagnostic_without_remote_body(monkeypatch):
    tool = WebGet()
    executor = ToolExecutor({tool.spec.name: tool})
    agent = ConversationOrchestrator(router=object(), tool_executor=executor)
    message, result, status = asyncio.run(agent._dispatch(
        "web_get", {"url": "ftp://example.com/file"},
    ))
    assert status == DispatchStatus.ERROR.value
    assert result is None
    assert "unsupported URL scheme" in message

    async def remote_failure(**_params):
        return ToolResult(tool_name="web_get", success=False,
                          content="private server response body")

    monkeypatch.setattr(tool, "execute", remote_failure)
    message, result, status = asyncio.run(agent._dispatch(
        "web_get", {"url": "https://example.com/"},
    ))
    assert status == DispatchStatus.ERROR.value
    assert result is None
    assert "private server response body" not in message
    assert "web_get request failed" in message


def test_pr_only_page_exposes_next_page_instead_of_terminal_empty(monkeypatch):
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    pull = {**_issue(9), "pull_request": {"url": "..."}}

    async def fake_get(path, params=None):
        return [pull, pull]

    monkeypatch.setattr(tool, "_get", fake_get)
    payload = json.loads(asyncio.run(tool.execute(limit=2, page=1)).content)
    assert payload["issues"] == []
    assert payload["next_page"] == 2


def test_issue_body_can_be_read_in_bounded_chunks(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")
    body = "A" * 6000 + "B" * 6000 + "C"

    async def fake_get(path, params=None):
        return _issue(17, body=body)

    monkeypatch.setattr(tool, "_get", fake_get)
    first = json.loads(asyncio.run(tool.execute(number=17)).content)
    second = json.loads(asyncio.run(tool.execute(number=17, body_offset=6000)).content)
    third = json.loads(asyncio.run(tool.execute(number=17, body_offset=12000)).content)
    assert first["body"] == "A" * 6000 and first["next_body_offset"] == 6000
    assert second["body"] == "B" * 6000 and second["next_body_offset"] == 12000
    assert third["body"] == "C" and third["next_body_offset"] is None


def test_long_comment_can_be_read_in_bounded_chunks(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")
    comment = "A" * 2000 + "B" * 2000 + "C"

    async def fake_get(path, params=None):
        if path.endswith("/comments"):
            return [{"id": 5, "body": comment, "user": {"login": "reporter"}}]
        return _issue(17, comments=1)

    monkeypatch.setattr(tool, "_get", fake_get)
    first = json.loads(asyncio.run(tool.execute(number=17, comment_limit=1)).content)
    second = json.loads(asyncio.run(tool.execute(
        number=17, comment_limit=1, comment_body_offset=2000,
    )).content)
    third = json.loads(asyncio.run(tool.execute(
        number=17, comment_limit=1, comment_body_offset=4000,
    )).content)
    assert first["comments"][0]["body"] == "A" * 2000
    assert first["comments"][0]["next_body_offset"] == 2000
    assert second["comments"][0]["body"] == "B" * 2000
    assert second["comments"][0]["next_body_offset"] == 4000
    assert third["comments"][0]["body"] == "C"
    assert third["comments"][0]["next_body_offset"] is None
    assert third["next_comment_page"] is None


def test_issue_body_and_comments_never_enter_executor_audit(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")
    marker = "PRIVATE-ISSUE-BODY-NOT-IN-AUDIT"

    async def fake_get(path, params=None):
        if path.endswith("/comments"):
            return [{"id": 1, "body": marker, "user": {"login": "reporter"}}]
        return _issue(17, body=marker, comments=1)

    monkeypatch.setattr(tool, "_get", fake_get)
    audit = []
    executor = ToolExecutor({tool.spec.name: tool}, audit=audit.append)
    dispatch = asyncio.run(executor.execute("github_get_issue", {"number": 17}))
    assert dispatch.status is DispatchStatus.OK
    assert marker in dispatch.result.content
    assert marker not in json.dumps(audit)
    assert audit[0]["result"] == "[untrusted GitHub issue content omitted]"


def test_invalid_arguments_do_not_enter_audit_or_make_network_calls(monkeypatch):
    marker = "PRIVATE-MODEL-ARGUMENT-NOT-IN-AUDIT"
    list_tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    get_tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")

    async def forbidden_get(*_args, **_kwargs):
        raise AssertionError("invalid input must not reach GitHub")

    monkeypatch.setattr(list_tool, "_get", forbidden_get)
    monkeypatch.setattr(get_tool, "_get", forbidden_get)
    audit = []
    executor = ToolExecutor({
        list_tool.spec.name: list_tool, get_tool.spec.name: get_tool,
    }, audit=audit.append)
    asyncio.run(executor.execute("github_list_issues", {"state": marker}))
    asyncio.run(executor.execute("github_get_issue", {"number": marker}))
    assert marker not in json.dumps(audit)


def test_github_read_client_ignores_proxy_environment_and_redirects(monkeypatch):
    observed = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [_issue(7)]

    class FakeClient:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers, params):
            assert url == "https://api.github.com/repos/acme/hive/issues"
            assert headers["Authorization"] == "Bearer test-token"
            return FakeResponse()

    monkeypatch.setattr("hive.tools.builtins.httpx.AsyncClient", FakeClient)
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    assert asyncio.run(tool.execute()).success
    assert observed[0]["trust_env"] is False
    assert observed[0]["follow_redirects"] is False


def test_get_issue_rejects_pull_request_without_loading_comments(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")
    observed = []

    async def fake_get(path, params=None):
        observed.append(path)
        return {**_issue(12, comments=5), "pull_request": {"url": "..."}}

    monkeypatch.setattr(tool, "_get", fake_get)
    result = asyncio.run(tool.execute(number=12))
    assert not result.success
    assert observed == ["/repos/acme/hive/issues/12"]


def test_broken_audit_projection_fails_closed_without_changing_tool_result(monkeypatch):
    tool = GitHubGetIssue(token="test-token", owner="acme", repo="hive")

    async def fake_get(path, params=None):
        return _issue(18, body="private issue text")

    def bad_audit_result(_result):
        raise RuntimeError("private issue text")

    monkeypatch.setattr(tool, "_get", fake_get)
    monkeypatch.setattr(tool, "audit_result", bad_audit_result)
    audit = []
    executor = ToolExecutor({tool.spec.name: tool}, audit=audit.append)
    dispatch = asyncio.run(executor.execute("github_get_issue", {"number": 18}))
    assert dispatch.status is DispatchStatus.OK
    assert "private issue text" in dispatch.result.content
    assert audit[0]["result"] == "[result redacted]"


def test_rate_limited_issue_read_does_not_complete_durable_heartbeat_task(
    tmp_path, monkeypatch,
):
    class Router:
        async def complete(self, messages, kind=None, **kwargs):
            return CompletionResult(text="ok", model="fake")

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    object.__setattr__(cfg, "autonomy_enabled", True)
    object.__setattr__(cfg, "approver_key", "test-approver-key")
    object.__setattr__(cfg, "worker_isolation", "required")
    hive = HiveOS.build(cfg, router=Router())
    tool = GitHubListIssues(token="test-token", owner="acme", repo="hive")
    request = httpx.Request("GET", "https://api.github.com/repos/acme/hive/issues")
    response = httpx.Response(429, request=request, headers={"Retry-After": "30"})

    async def rate_limited(*_args, **_kwargs):
        raise httpx.HTTPStatusError("remote private text", request=request, response=response)

    monkeypatch.setattr(tool, "_get", rate_limited)
    hive.tool_executor.add_tool(tool)
    task_id = hive.task_board.enqueue(
        "tool", {"tool": "github_list_issues", "args": {"state": "open"}},
        source="test", max_attempts=2,
    )
    summary = asyncio.run(Heartbeat(hive, goals=["g"]).tick())
    task = hive.task_board.get(task_id)
    assert summary["dispatched"] == 0
    assert task is not None and task.state != "done"
    assert task.attempts == 1
    assert task.rate_limit_count == 1
    assert task.scheduled_for >= time.time() + 20
    assert TaskBoard(cfg.state_db).get(task_id).scheduled_for == task.scheduled_for
    assert asyncio.run(Heartbeat(hive, goals=["g"]).tick())["dispatched"] == 0
    assert hive.task_board.get(task_id).attempts == 1
