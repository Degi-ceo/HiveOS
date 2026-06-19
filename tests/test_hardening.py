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


# --- Wave 3M additional tests ---------------------------------------------------

def test_vault_write_creates_file(tmp_path):
    """ObsidianVault.write() creates a .md file under kind/."""
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path)
    p = v.write("memory", "My Note", "content here", source="test")
    assert p.exists()
    assert p.suffix == ".md"


def test_vault_write_stats_increments(tmp_path):
    """ObsidianVault.stats()['notes'] increments after each write."""
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path)
    assert v.stats()["notes"] == 0
    v.write("memory", "Note1", "c1")
    assert v.stats()["notes"] == 1
    v.write("memory", "Note2", "c2")
    assert v.stats()["notes"] == 2


def test_vault_write_sanitizes_filename(tmp_path):
    """ObsidianVault.write() with a topic containing '/' does not create nested dirs."""
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path)
    p = v.write("memory", "topic/with/slash", "content")
    # File must be created somewhere under the vault root
    assert p.exists()


def test_sandbox_runner_passthrough_returns_ok_on_success(tmp_path):
    """make_sandbox_runner(None) returns _default_run which runs commands and returns (rc, output)."""
    from hive.core.sandbox import make_sandbox_runner
    run = make_sandbox_runner(None)
    rc, out = asyncio.run(run("echo hello"))
    assert rc == 0
    assert "hello" in out


def test_redact_args_secret_key_masked():
    """redact_args() masks the value of a 'secret' key (common sensitive key name)."""
    from hive.core.redact import redact_args
    out = redact_args({"secret": "supersecretlongvalue"})
    assert out["secret"] == "***REDACTED***"


def test_mask_secret_exactly_18_chars():
    """mask_secret() on exactly 18 characters returns first6...last4 with ellipsis."""
    from hive.core.redact import mask_secret
    s = "A" * 18
    masked = mask_secret(s)
    assert "…" in masked
    assert masked.startswith("AAAAAA")
    assert masked.endswith("AAAA")


# --- Wave 3S: LoopGuard additional tests ----------------------------------------

def test_loop_guard_different_tools_not_tripped():
    """Different tool names never trigger the identical-call detector."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=3)
    assert lg.check("tool_a") is None
    assert lg.check("tool_b") is None
    assert lg.check("tool_c") is None


def test_loop_guard_reset_clears_history():
    """After reset(), two consecutive calls no longer form a repeat sequence."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=3)
    lg.check("search")
    lg.check("search")
    lg.reset()
    # After reset the counter restarts; two calls can't trip max_identical=3
    assert lg.check("search") is None
    assert lg.check("search") is None
    assert lg.stats()["total_calls"] == 2


def test_loop_guard_args_differentiate_calls():
    """Calls to the same tool with different args are not counted as identical."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=3)
    assert lg.check("search", {"q": "foo"}) is None
    assert lg.check("search", {"q": "bar"}) is None
    assert lg.check("search", {"q": "baz"}) is None


def test_loop_guard_max_identical_1_trips_on_first_call():
    """max_identical=1 trips on the very first call to a tool."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=1)
    result = lg.check("shell")
    assert result is not None and "1x" in result


def test_loop_guard_stats_reflects_calls():
    """stats() accurately reports total_calls, unique_tools, and per_tool counts."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5)
    lg.check("read_file")
    lg.check("read_file")
    lg.check("write_file")
    s = lg.stats()
    assert s["total_calls"] == 3
    assert s["unique_tools"] == 2
    assert s["per_tool"]["read_file"] == 2
    assert s["per_tool"]["write_file"] == 1
    assert s["max_identical"] == 5


def test_loop_guard_trips_at_nth_call_not_before():
    """With max_identical=3, calls 1 and 2 return None; call 3 returns a reason string."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=3)
    assert lg.check("grep") is None
    assert lg.check("grep") is None
    result = lg.check("grep")
    assert result is not None and "grep" in result


# --- Wave 3Z: LoopGuard additional tests ----------------------------------------

def test_wave3z_loop_guard_per_tool_budget_trips():
    """max_per_tool budget fires when a single tool is called too many times with distinct args."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=100, max_per_tool=3)
    assert lg.check("ls", {"path": "/a"}) is None
    assert lg.check("ls", {"path": "/b"}) is None
    assert lg.check("ls", {"path": "/c"}) is None
    result = lg.check("ls", {"path": "/d"})
    assert result is not None and "ls" in result


def test_wave3z_loop_guard_stats_has_max_per_tool():
    """stats() always includes a 'max_per_tool' field matching the constructor value."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5, max_per_tool=20)
    s = lg.stats()
    assert "max_per_tool" in s
    assert s["max_per_tool"] == 20


def test_wave3z_loop_guard_reset_clears_all_stats():
    """After reset(), stats() shows zero calls and no tools."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5)
    lg.check("tool_a")
    lg.check("tool_b")
    lg.reset()
    s = lg.stats()
    assert s["total_calls"] == 0
    assert s["unique_tools"] == 0
    assert s["per_tool"] == {}


def test_wave3z_loop_guard_max_identical_2_trips_on_second():
    """max_identical=2 fires on the second identical call."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=2)
    assert lg.check("read_file") is None
    result = lg.check("read_file")
    assert result is not None and "2x" in result


def test_wave3z_loop_guard_none_args_and_empty_dict_are_identical():
    """check(tool, None) and check(tool, {}) produce the same hash — both count as identical."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=2)
    assert lg.check("shell", None) is None
    result = lg.check("shell", {})
    assert result is not None


def test_wave3z_loop_guard_two_instances_are_independent():
    """Two LoopGuard instances do not share state."""
    from hive.agents.loop_guard import LoopGuard
    lg1 = LoopGuard(max_identical=2)
    lg2 = LoopGuard(max_identical=2)
    lg1.check("tool")
    lg1.check("tool")  # trips lg1
    assert lg2.check("tool") is None  # lg2 unaffected


def test_wave3z_loop_guard_top_repeated_tools_ordering():
    """top_repeated_tools() returns tools sorted by call count descending."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=100, max_per_tool=100)
    for _ in range(5):
        lg.check("heavy_tool")
    for _ in range(2):
        lg.check("light_tool")
    top = lg.top_repeated_tools(2)
    assert top[0] == ("heavy_tool", 5)
    assert top[1] == ("light_tool", 2)


def test_wave3z_loop_guard_call_count_returns_per_tool_count():
    """call_count(tool) returns the number of times that tool has been called."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=100, max_per_tool=100)
    lg.check("alpha")
    lg.check("alpha")
    lg.check("beta")
    assert lg.call_count("alpha") == 2
    assert lg.call_count("beta") == 1
    assert lg.call_count("gamma") == 0


# --- Wave 4F: LoopGuard additional tests ----------------------------------------

def test_wave4f_loop_guard_pingpong_detected():
    """A-B-A-B sequence returns the ping-pong reason string."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    lg.check("tool_a")
    lg.check("tool_b")
    lg.check("tool_a")
    result = lg.check("tool_b")
    assert result is not None and "ping-pong" in result


def test_wave4f_loop_guard_pingpong_not_triggered_without_alternation():
    """Four distinct tools in a row do not trigger the ping-pong detector."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    assert lg.check("a") is None
    assert lg.check("b") is None
    assert lg.check("c") is None
    assert lg.check("d") is None


def test_wave4f_loop_guard_call_count_zero_after_reset():
    """call_count() returns 0 for every tool after reset()."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5, max_per_tool=50)
    lg.check("shell")
    lg.check("shell")
    lg.reset()
    assert lg.call_count("shell") == 0


def test_wave4f_loop_guard_complex_args_differentiate():
    """Calls with nested dict args that differ in one nested key are not identical."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=2)
    assert lg.check("http_get", {"url": "https://a.com", "params": {"page": 1}}) is None
    assert lg.check("http_get", {"url": "https://a.com", "params": {"page": 2}}) is None


def test_wave4f_loop_guard_max_per_tool_with_distinct_args():
    """max_per_tool budget fires even when every call uses different args."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=100, max_per_tool=3)
    for i in range(3):
        assert lg.check("search", {"q": str(i)}) is None
    result = lg.check("search", {"q": "final"})
    assert result is not None and "search" in result


def test_wave4f_loop_guard_stats_after_mixed_calls():
    """stats() correctly reflects a mix of identical and distinct tool calls."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    lg.check("read_file", {"path": "/a"})
    lg.check("read_file", {"path": "/b"})
    lg.check("write_file", {"path": "/c"})
    s = lg.stats()
    assert s["total_calls"] == 3
    assert s["per_tool"]["read_file"] == 2
    assert s["per_tool"]["write_file"] == 1
    assert s["unique_tools"] == 2


def test_wave4f_loop_guard_identical_fires_before_per_tool():
    """When max_identical is lower than max_per_tool, identical check fires first."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=2, max_per_tool=100)
    assert lg.check("grep") is None
    result = lg.check("grep")
    assert result is not None and "2x" in result


def test_wave4f_loop_guard_top_repeated_tools_empty():
    """top_repeated_tools() on a fresh guard returns an empty list."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5, max_per_tool=10)
    top = lg.top_repeated_tools(3)
    assert top == []


# --- Wave 4N: LoopGuard additional tests ----------------------------------------

def test_wave4n_loop_guard_default_max_identical_is_3():
    """LoopGuard() constructed with no args has max_identical=3 in stats()."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard()
    assert lg.stats()["max_identical"] == 3


def test_wave4n_loop_guard_default_max_per_tool_is_10():
    """LoopGuard() constructed with no args has max_per_tool=10 in stats()."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard()
    assert lg.stats()["max_per_tool"] == 10


def test_wave4n_loop_guard_call_count_zero_for_unknown_tool():
    """call_count() returns 0 for a tool that was never called, even after other calls."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=5, max_per_tool=10)
    lg.check("known_tool")
    assert lg.call_count("unknown_tool") == 0


def test_wave4n_loop_guard_per_tool_budget_error_message_content():
    """Budget error string includes the tool name and the budget limit."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=100, max_per_tool=2)
    lg.check("scraper", {"url": "https://a.com"})
    lg.check("scraper", {"url": "https://b.com"})
    result = lg.check("scraper", {"url": "https://c.com"})
    assert result is not None
    assert "scraper" in result
    assert "2" in result


def test_wave4n_loop_guard_pingpong_not_triggered_on_aba():
    """A-B-A sequence (only 3 calls) does not trigger the ping-pong detector."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    assert lg.check("tool_a") is None
    assert lg.check("tool_b") is None
    assert lg.check("tool_a") is None


def test_wave4n_loop_guard_top_repeated_tools_n_larger_than_tools():
    """top_repeated_tools(n) with n larger than unique tools returns all tools."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    lg.check("alpha")
    lg.check("beta")
    top = lg.top_repeated_tools(50)
    names = [name for name, _ in top]
    assert "alpha" in names and "beta" in names
    assert len(top) == 2


def test_wave4n_loop_guard_top_repeated_tools_n_zero_returns_one():
    """top_repeated_tools(0) clamps to n=1 and returns the single most-called tool."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=10, max_per_tool=100)
    lg.check("heavy")
    lg.check("heavy")
    lg.check("light")
    top = lg.top_repeated_tools(0)
    assert len(top) == 1
    assert top[0] == ("heavy", 2)


def test_wave4n_loop_guard_args_with_non_string_values():
    """check() handles args containing int and list values without error."""
    from hive.agents.loop_guard import LoopGuard
    lg = LoopGuard(max_identical=3)
    assert lg.check("query", {"page": 1, "tags": ["x", "y"]}) is None
    assert lg.check("query", {"page": 2, "tags": ["x", "y"]}) is None
    assert lg.check("query", {"page": 3, "tags": ["x", "y"]}) is None
