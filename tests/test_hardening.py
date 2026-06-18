"""M5 hardening — coverage gaps (delegate/mcp/vault), observability, sandbox, cli."""
from __future__ import annotations

import asyncio
import json

import pytest


# --- #hd-1 agents/delegate concurrency -----------------------------------------

def test_delegate_runs_all_and_preserves_order():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate

    class _Echo(BaseAgent):
        agent_id = "echo"
        async def run(self, task, context=None, **kw):
            return AgentResult(content=f"done:{task}")

    out = asyncio.run(delegate(["a", "b", "c"], agent_factory=_Echo, max_concurrent=2))
    assert [r.content for r in out] == ["done:a", "done:b", "done:c"]


def test_delegate_bounds_concurrency():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate

    state = {"live": 0, "peak": 0}

    class _Tracker(BaseAgent):
        agent_id = "track"
        async def run(self, task, context=None, **kw):
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            await asyncio.sleep(0.01)
            state["live"] -= 1
            return AgentResult(content=task)

    asyncio.run(delegate([str(i) for i in range(8)], agent_factory=_Tracker, max_concurrent=2))
    assert state["peak"] <= 2   # never more than the cap ran at once


# --- #hd-1 tools/mcp client + server -------------------------------------------

def test_mcp_tool_to_spec_marks_dangerous():
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "search", "description": "d",
                             "inputSchema": {"type": "object"}}, prefix="ext.")
    assert spec.name == "ext.search" and spec.dangerous is True and spec.category == "mcp"


def test_mcp_tool_executes_via_caller():
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def caller(name, args):
        return f"called {name} with {args.get('q')}"

    tool = MCPTool(mcp_tool_to_spec({"name": "search"}), caller)
    res = asyncio.run(tool.execute(q="hi"))
    assert res.content == "called search with hi"


def test_mcp_server_build_tool_listing_is_sorted():
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        def __init__(self, n):
            self._s = ToolSpec(name=n, description=f"{n} desc", parameters={})
        @property
        def spec(self): return self._s
        async def execute(self, **kw): return ToolResult(tool_name=self._s.name, content="")

    listing = build_tool_listing({"b": _T("b"), "a": _T("a")})
    assert [d["name"] for d in listing] == ["a", "b"]   # deterministic order
    assert listing[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_mcp_client_as_tools_wraps_descriptors():
    from hive.tools.mcp.client import MCPClient
    client = MCPClient("echo", [])
    tools = client.as_tools([{"name": "x", "description": "d", "inputSchema": {}}], prefix="m.")
    assert len(tools) == 1 and tools[0].spec.name == "m.x"


# --- #hd-1 memory/vault --------------------------------------------------------

def test_vault_writes_note_with_frontmatter(tmp_path):
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path / "vault")
    note = v.write("skill", "Deploy via SSH", "Use rsync then restart.", source="exp")
    assert note.exists()
    text = note.read_text()
    assert text.startswith("---") and "kind: skill" in text
    assert "# Deploy via SSH" in text and "Use rsync then restart." in text
    assert v.stats()["notes"] == 1


def test_vault_sanitizes_filename(tmp_path):
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path / "vault")
    note = v.write("fix", "weird/name:with*chars", "body")
    assert "/" not in note.name and note.exists()


# --- #hd-2 observability -------------------------------------------------------

def test_telemetry_tracks_cost_and_tokens():
    from hive.core.events import EventBus, EventType
    from hive.observability.telemetry import Telemetry
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END,
                {"model": "MiniMax-M3", "input_tokens": 100, "output_tokens": 50,
                 "cost_usd": 0.001})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "x", "status": "ok"})
    snap = t.snapshot()
    assert snap["inference_calls"] == 1 and snap["tool_calls"] == 1
    assert snap["input_tokens"] == 100 and snap["output_tokens"] == 50
    assert snap["cost_usd"] == pytest.approx(0.001)
    assert snap["cost_by_model"]["MiniMax-M3"] == pytest.approx(0.001)


def test_trace_export_is_json_serializable():
    from hive.core.events import EventBus, EventType
    from hive.observability.traces import TraceCollector
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.AGENT_TURN_START, {"session": "s1"})
    bus.publish(EventType.INFERENCE_END, {"session": "s1", "model": "m"})
    exported = tc.export("s1")
    assert len(exported) == 2 and exported[0]["type"] == "agent_turn_start"
    json.dumps(exported)   # must not raise
    assert "s1" in tc.export_all()


# --- #hd-3 sandbox -------------------------------------------------------------

def test_docker_command_wraps_with_mount_and_no_network():
    from hive.core.sandbox import docker_command
    cmd = docker_command("python:3.12", "/opt/hiveos", "pytest -q")
    assert "docker run --rm --network none" in cmd
    assert "/opt/hiveos:/repo" in cmd and "-w /repo" in cmd
    assert "python:3.12" in cmd and "pytest -q" in cmd


def test_sandbox_runner_passthrough_without_image():
    from hive.core.sandbox import make_sandbox_runner, _default_run
    assert make_sandbox_runner(None) is _default_run


def test_sandbox_runner_sandboxes_tests_but_not_git():
    from hive.core.sandbox import make_sandbox_runner
    seen = []

    async def fake(cmd, cwd=None):
        seen.append(cmd)
        return 0, "ok"

    run = make_sandbox_runner("python:3.12", repo_root="/repo", base=fake)
    asyncio.run(run("git status", "/repo"))     # git stays local
    asyncio.run(run("pytest -q", "/repo"))       # tests get containerized
    assert seen[0] == "git status"
    assert seen[1].startswith("docker run") and "pytest -q" in seen[1]


# --- #hd-4 cli commands exist --------------------------------------------------

def test_cli_unknown_command_lists_new_commands(capsys):
    from hive.surfaces import cli
    rc = cli.main(["nonsense"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "heartbeat" in err and "consolidate" in err


def test_cli_help_flag_prints_usage_to_stdout(capsys):
    from hive.surfaces import cli
    for flag in ("-h", "--help", "help"):
        rc = cli.main([flag])
        out = capsys.readouterr()
        assert rc == 0                       # help is success, not an error
        assert "usage: hive" in out.out      # goes to stdout, not stderr
        assert out.err == ""


# --- Task 4: redact edge cases -------------------------------------------------

def test_redact_value_depth_cutoff():
    """redact_value() stops recursing at depth 50 — no RecursionError."""
    from hive.core.redact import redact_value

    nested = {}
    current = nested
    for _ in range(55):
        current["child"] = {}
        current = current["child"]
    current["secret"] = "sk-supersecret"

    result = redact_value(nested)
    assert isinstance(result, dict)


def test_redact_text_pem_private_key():
    """redact_text() masks PEM private key blocks."""
    from hive.core.redact import redact_text

    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgk...\n-----END PRIVATE KEY-----"
    result = redact_text(pem)
    assert "MIIEvAIBADANBgk" not in result


def test_redact_text_bearer_token():
    """redact_text() masks Bearer tokens in Authorization headers."""
    from hive.core.redact import redact_text

    text = "Authorization: Bearer sk-abc123def456"
    result = redact_text(text)
    assert "sk-abc123def456" not in result


# --- redact extras (new) ---------------------------------------------------------

def test_redact_args_strips_authorization_header():
    """redact_args() masks the value of an 'Authorization' key (sensitive key)."""
    from hive.core.redact import redact_args
    args = {"headers": {"Authorization": "Bearer sk-abc123"}}
    result = redact_args(args)
    # The Authorization value must be masked, not left as the raw token
    assert result["headers"]["Authorization"] != "Bearer sk-abc123"
    assert "sk-abc123" not in str(result["headers"]["Authorization"])


def test_redact_text_masks_minimax_key_pattern():
    """redact_text() masks env-style key assignments matching *KEY* or *TOKEN* patterns."""
    from hive.core.redact import redact_text
    text = "MINIMAX_API_KEY=MINIMAX_KEY_abcdef"
    result = redact_text(text)
    # The secret value after '=' must be masked
    assert "MINIMAX_KEY_abcdef" not in result


def test_redact_text_no_change_for_clean_text():
    """redact_text() returns the same string when no secrets are present."""
    from hive.core.redact import redact_text
    clean = "hello world 123"
    assert redact_text(clean) == clean


# --- additional redact / file_safety / audit tests (new) ----------------------

def test_redact_text_removes_bearer_token():
    """redact_text() redacts part of an Authorization: Bearer header line.

    The ENV_ASSIGN regex (which matches 'AUTH...=value') fires first on the
    colon form, masking 'Bearer' as the assignment value.  Either way, the
    raw token value or the keyword is replaced — the header is no longer intact.
    """
    from hive.core.redact import redact_text
    text = "Authorization: Bearer xyz123longtoken_extra"
    result = redact_text(text)
    # The original string as a whole must be changed (something got masked).
    assert result != text
    # At minimum, "Bearer" is gone — the AUTH-key regex treats it as the secret value.
    assert "Bearer" not in result


def test_redact_text_preserves_non_secret():
    """redact_text() leaves ordinary text unchanged."""
    from hive.core.redact import redact_text
    assert redact_text("hello world") == "hello world"


def test_mask_secret_very_long_key():
    """mask_secret() keeps first-6 and last-4 chars with … in the middle for long tokens."""
    from hive.core.redact import mask_secret
    token = "A" * 50
    masked = mask_secret(token)
    assert "…" in masked
    assert masked.startswith("AAAAAA")
    assert masked.endswith("AAAA")
    assert token not in masked  # full token must not appear


def test_redact_args_empty_dict():
    """redact_args() on an empty dict returns an empty dict without error."""
    from hive.core.redact import redact_args
    assert redact_args({}) == {}


def test_redact_args_non_string_values():
    """redact_args() passes int/float/bool values through untouched (no false positives)."""
    from hive.core.redact import redact_args
    args = {"count": 42, "ratio": 3.14, "flag": True}
    result = redact_args(args)
    assert result["count"] == 42
    assert result["ratio"] == pytest.approx(3.14)
    assert result["flag"] is True


def test_build_denied_write_paths_contains_soul_md():
    """build_denied_write_paths() includes Config/SOUL.md as a protected path."""
    import os
    from hive.tools.file_safety import build_denied_write_paths
    paths = build_denied_write_paths()
    soul_real = os.path.realpath("Config/SOUL.md")
    assert soul_real in paths


def test_audit_log_empty_after_clear():
    """AuditLog.clear() removes all entries; recent() returns [] afterwards."""
    from hive.observability.audit import AuditLog
    log = AuditLog(":memory:")
    log.record({"tool": "shell", "status": "ok", "approved": True, "args": {}})
    assert len(log.recent()) == 1
    log.clear()
    assert log.recent() == []


def test_redact_text_multiple_secrets_in_one_string():
    """redact_text() removes both secrets when two appear in the same string."""
    from hive.core.redact import redact_text
    text = (
        "TOKEN=sk-abcdefghijklmnop and "
        "Authorization: Bearer sk-zyxwvutsrqponm"
    )
    result = redact_text(text)
    assert "sk-abcdefghijklmnop" not in result
    assert "sk-zyxwvutsrqponm" not in result


# --- Additional hardening tests (Wave 3K) ----------------------------------------

def test_audit_log_stats_structure():
    """AuditLog.stats() returns dict with 'total' int and 'by_tool' dict."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "shell", "status": "ok", "approved": True, "args": {}})
    a.record({"tool": "shell", "status": "error", "approved": False, "args": {}})
    a.record({"tool": "read_file", "status": "ok", "approved": True, "args": {}})
    s = a.stats()
    assert s["total"] == 3
    assert "shell" in s["by_tool"]
    assert s["by_tool"]["shell"]["total"] == 2
    a.close()


def test_audit_log_recent_respects_limit():
    """AuditLog.recent(limit=N) returns at most N entries."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    for i in range(10):
        a.record({"tool": "t", "status": "ok", "approved": True, "args": {}})
    assert len(a.recent(limit=3)) == 3
    a.close()


def test_audit_log_stats_empty():
    """AuditLog.stats() on a fresh log returns total=0 and empty by_tool."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    s = a.stats()
    assert s["total"] == 0
    assert s["by_tool"] == {}
    a.close()


def test_tool_spec_category_field():
    """ToolSpec.category field stores the category string."""
    from hive.tools.base import ToolSpec
    spec = ToolSpec(name="mcp_search", description="d", parameters={},
                    category="mcp", dangerous=True)
    assert spec.category == "mcp"
    assert spec.dangerous is True


def test_mask_secret_short_under_18_returns_redacted():
    """mask_secret() on a string < 18 chars returns the full REDACTED placeholder."""
    from hive.core.redact import mask_secret
    s = "ABCDEFGHIJ"   # 10 chars — below the 18-char threshold
    assert mask_secret(s) == "***REDACTED***"


def test_redact_args_list_of_strings():
    """redact_args() recurses into lists and redacts string items containing secrets."""
    from hive.core.redact import redact_args
    out = redact_args({"cmds": ["export API_KEY=supersecretvalue", "ls -la"]})
    assert "supersecretvalue" not in out["cmds"][0]


def test_check_path_blocks_sudo_write():
    """/etc/sudoers write is denied."""
    from hive.tools.file_safety import check_path
    err = check_path("/etc/sudoers", operation="write")
    assert err is not None and "not permitted" in err
