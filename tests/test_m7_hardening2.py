"""M7 — security & robustness: redact, protocol versioning, tool availability, titles."""
from __future__ import annotations

import asyncio
import json

import pytest


# --- B2 redact -----------------------------------------------------------------

from hive.core.redact import mask_secret, redact_text, redact_args


def test_mask_secret_short_vs_long():
    assert mask_secret("short") == "***REDACTED***"
    masked = mask_secret("sk-abcdefghijklmnopqrstuvwxyz")
    assert masked.startswith("sk-abc") and masked.endswith("wxyz") and "…" in masked


def test_redact_text_masks_common_shapes():
    assert "secretval" not in redact_text("MINIMAX_API_KEY=secretval")
    assert "***REDACTED***" in redact_text("Authorization: Bearer abc.def.ghi")
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20  # realistic-length JWT
    assert jwt not in redact_text(f"token {jwt} here")
    sk = "sk-ABCDEFGHIJKLMNOPQRSTUV"
    assert sk not in redact_text(f"key={sk}")


def test_redact_args_masks_sensitive_keys_keeps_others():
    out = redact_args({"api_key": "supersecretvalue", "path": "/tmp/x", "count": 3})
    assert out["api_key"] == "***REDACTED***"
    assert out["path"] == "/tmp/x" and out["count"] == 3


def test_redact_args_recurses_and_scrubs_strings():
    out = redact_args({"nested": {"password": "hunter2longvalue", "ok": "fine"},
                       "cmd": "export TOKEN=abcdef1234567890zzz"})
    assert out["nested"]["password"] == "***REDACTED***"
    assert out["nested"]["ok"] == "fine"
    assert "abcdef1234567890zzz" not in out["cmd"]


def test_audit_log_redacts_args():
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "shell", "status": "ok", "args": {"api_key": "topsecretvalue"}})
    row = a._db.execute("SELECT args FROM audit_log").fetchone()
    assert "topsecretvalue" not in row["args"] and "REDACTED" in row["args"]
    a.close()


# --- B4 protocol versioning ----------------------------------------------------

def test_protocol_version_on_response_and_health():
    from hive.gateway.protocol import PROTOCOL_VERSION, ChatResponse
    assert ChatResponse(reply="x", session_id="s").protocol_version == PROTOCOL_VERSION

    from starlette.testclient import TestClient
    from hive.core.config import HiveConfig
    from hive.gateway.app import create_app
    from hive.runtime import HiveOS
    from hive.llm.adapters.base import CompletionResult

    class _R:
        async def complete(self, *a, **k): return CompletionResult(text="x", model="m")
        async def aclose(self): pass

    hive = HiveOS.build(HiveConfig.from_env(root="/tmp/hv_m7", load_dotenv=False), router=_R())
    with TestClient(create_app(hive)) as c:
        assert c.get("/health").json()["protocol_version"] == PROTOCOL_VERSION


# --- B5 tool availability ------------------------------------------------------

from hive.tools.base import BaseTool, ToolSpec
from hive.core.types import ToolResult


class _Unavailable(BaseTool):
    spec = ToolSpec(name="needs_key", description="d", parameters={})
    async def execute(self, **kw): return ToolResult(tool_name="needs_key", content="ran")
    def available(self): return False


class _Available(BaseTool):
    spec = ToolSpec(name="ready", description="d", parameters={})
    async def execute(self, **kw): return ToolResult(tool_name="ready", content="ran")


def test_default_tool_is_available():
    assert _Available().available() is True


def test_unavailable_tool_hidden_from_schemas():
    from hive.agents.orchestrator import ConversationOrchestrator

    class _R:
        async def complete(self, *a, **k): pass
    orch = ConversationOrchestrator(_R(), tools={"needs_key": _Unavailable(),
                                                 "ready": _Available()})
    names = [s["name"] for s in (orch._tool_schemas() or [])]
    assert "ready" in names and "needs_key" not in names


def test_executor_refuses_unavailable_tool():
    from hive.tools.executor import ToolExecutor, DispatchStatus

    class _Gate:
        def is_dangerous(self, *a, **k): return False
    ex = ToolExecutor({"needs_key": _Unavailable()}, gate=_Gate())
    d = asyncio.run(ex.execute("needs_key", {}))
    assert d.status is DispatchStatus.ERROR and "unavailable" in d.error


# --- B3 title_session ----------------------------------------------------------

def test_title_session_generates_and_is_idempotent(tmp_path, monkeypatch):
    from hive.core.config import HiveConfig
    from hive.core.types import Role
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS

    calls = {"n": 0}

    class _TitleRouter:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            calls["n"] += 1
            return CompletionResult(text="Deploy Pipeline Help", model="m")
        async def aclose(self): pass

    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                     router=_TitleRouter())
    h.session_store.append("s1", Role.USER, "help me fix the deploy pipeline")

    t1 = asyncio.run(h.title_session("s1"))
    assert t1 == "Deploy Pipeline Help"
    assert h.session_store.get_title("s1") == "Deploy Pipeline Help"
    n_after_first = calls["n"]
    t2 = asyncio.run(h.title_session("s1"))      # idempotent: no regeneration
    assert t2 == "Deploy Pipeline Help" and calls["n"] == n_after_first
    asyncio.run(h.aclose())


def test_title_session_none_for_empty(tmp_path, monkeypatch):
    from hive.core.config import HiveConfig
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS

    class _R:
        async def complete(self, *a, **k): return CompletionResult(text="x", model="m")
        async def aclose(self): pass
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_R())
    assert asyncio.run(h.title_session("empty")) is None
    asyncio.run(h.aclose())


# --- Additional M7 hardening tests -----------------------------------------------

def test_redact_args_nested_api_key():
    from hive.core.redact import redact_args
    out = redact_args({"outer": {"inner": {"api_key": "supersecret123"}}})
    assert out["outer"]["inner"]["api_key"] == "***REDACTED***"


def test_redact_args_list_values_not_redacted_unless_string():
    from hive.core.redact import redact_args
    out = redact_args({"items": [1, 2, 3], "safe": "hello"})
    assert out["items"] == [1, 2, 3]  # non-string list values pass through


def test_protocol_version_is_semver_like():
    from hive.gateway.protocol import PROTOCOL_VERSION
    import re
    assert re.match(r"^\d+\.\d+$", PROTOCOL_VERSION), \
        f"PROTOCOL_VERSION should be semver-like (got {PROTOCOL_VERSION!r})"


def test_tool_spec_dangerous_field_present():
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _Safe(BaseTool):
        spec = ToolSpec(name="safe_tool", description="d", parameters={}, dangerous=False)
        async def execute(self, **kw): return ToolResult(tool_name="safe_tool", content="ok")

    class _Dangerous(BaseTool):
        spec = ToolSpec(name="danger_tool", description="d", parameters={}, dangerous=True)
        async def execute(self, **kw): return ToolResult(tool_name="danger_tool", content="ok")

    assert _Safe().spec.dangerous is False
    assert _Dangerous().spec.dangerous is True


# --- Additional M7 hardening tests (new) ----------------------------------------

def test_redact_args_password_key_redacted():
    """{'password': 'secret'} → value replaced with REDACTED."""
    from hive.core.redact import redact_args
    out = redact_args({"password": "supersecretvalue"})
    assert out["password"] == "***REDACTED***"


def test_redact_args_token_key_redacted():
    """{'token': 'mytokenvalue'} → value replaced with REDACTED."""
    from hive.core.redact import redact_args
    out = redact_args({"token": "mytokenvalue_longerthan18chars"})
    assert out["token"] == "***REDACTED***"


def test_audit_log_record_approved_false():
    """An audit record without approved=True persists approved=0 (False)."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "ping", "status": "ok", "args": {}})
    row = a._db.execute("SELECT approved FROM audit_log").fetchone()
    assert row["approved"] == 0
    a.close()


def test_chat_response_has_session_id():
    """ChatResponse stores and exposes the session_id that was passed in."""
    from hive.gateway.protocol import ChatResponse
    resp = ChatResponse(reply="hello", session_id="abc-123")
    assert resp.session_id == "abc-123"
    assert resp.reply == "hello"


def test_tool_executor_error_for_unavailable():
    """DispatchStatus.ERROR is returned when the tool reports itself unavailable."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _NotReady(BaseTool):
        spec = ToolSpec(name="not_ready", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="not_ready", content="x")
        def available(self): return False

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({"not_ready": _NotReady()}, gate=_Gate())
    d = asyncio.run(ex.execute("not_ready", {}))
    assert d.status is DispatchStatus.ERROR
    assert "unavailable" in (d.error or "")


def test_executor_raises_on_completely_missing_tool():
    """execute('nonexistent', {}) returns DispatchStatus.ERROR for an unknown tool."""
    from hive.tools.executor import ToolExecutor, DispatchStatus

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({}, gate=_Gate())
    d = asyncio.run(ex.execute("nonexistent", {}))
    assert d.status is DispatchStatus.ERROR
    assert d.error is not None and "nonexistent" in d.error


# --- New tests (6) ---------------------------------------------------------------

def test_redact_text_leaves_plain_string_untouched():
    """A string with no secret shapes must pass through redact_text unchanged."""
    from hive.core.redact import redact_text
    plain = "hello world, nothing secret here"
    assert redact_text(plain) == plain


def test_mask_secret_short_token_fully_masked():
    """A token shorter than 18 chars must be fully replaced with ***REDACTED***."""
    from hive.core.redact import mask_secret
    assert mask_secret("short") == "***REDACTED***"


def test_executor_add_and_has_tool():
    """add_tool() registers a tool; has_tool() returns True for it afterward."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _New(BaseTool):
        spec = ToolSpec(name="new_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="new_tool", content="ok")

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({}, gate=_Gate())
    assert not ex.has_tool("new_tool")
    ex.add_tool(_New())
    assert ex.has_tool("new_tool")


def test_executor_list_tools_sorted():
    """list_tools() must return tool names in alphabetical order."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _A(BaseTool):
        spec = ToolSpec(name="alpha", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="alpha", content="x")

    class _Z(BaseTool):
        spec = ToolSpec(name="zeta", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="zeta", content="x")

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({"zeta": _Z(), "alpha": _A()}, gate=_Gate())
    names = ex.list_tools()
    assert names == sorted(names)
    assert "alpha" in names and "zeta" in names


def test_audit_log_count_increments():
    """Each call to record() must increment the total count by exactly one."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    assert a.count() == 0
    a.record({"tool": "ping", "status": "ok", "args": {}})
    assert a.count() == 1
    a.record({"tool": "pong", "status": "ok", "args": {}})
    assert a.count() == 2
    a.close()


def test_chat_request_session_id_default():
    """ChatRequest must default session_id to 'default' when not provided."""
    from hive.gateway.protocol import ChatRequest
    req = ChatRequest(message="hello")
    assert req.session_id == "default"


# ---------------------------------------------------------------------------
# Batch 5 — six additional tests
# ---------------------------------------------------------------------------

def test_redact_value_dict_recursion():
    """redact_value() on a nested dict applies masking at every level."""
    from hive.core.redact import redact_value
    result = redact_value({"a": {"api_key": "supersecretvalue", "safe": "ok"}})
    assert result["a"]["api_key"] == "***REDACTED***"
    assert result["a"]["safe"] == "ok"


def test_redact_text_private_key_masked():
    """redact_text() must mask PEM private-key blocks."""
    from hive.core.redact import redact_text
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    result = redact_text(pem)
    assert "PRIVATE KEY" not in result
    assert "***REDACTED***" in result


def test_audit_log_stats_by_tool():
    """stats() must include per-tool totals after recording entries."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "alpha", "status": "ok", "args": {}})
    a.record({"tool": "alpha", "status": "ok", "args": {}})
    a.record({"tool": "beta", "status": "error", "args": {}})
    s = a.stats()
    assert s["total"] == 3
    assert s["by_tool"]["alpha"]["total"] == 2
    assert s["by_tool"]["beta"]["total"] == 1
    a.close()


def test_audit_log_clear_empties_table():
    """clear() must remove all audit entries so count() returns 0."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "t", "status": "ok", "args": {}})
    a.record({"tool": "t", "status": "ok", "args": {}})
    assert a.count() == 2
    a.clear()
    assert a.count() == 0
    a.close()


def test_executor_remove_tool_returns_true_then_false():
    """remove_tool() returns True the first time and False if called again."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="removable", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="removable", content="x")

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({"removable": _T()}, gate=_Gate())
    assert ex.remove_tool("removable") is True
    assert ex.remove_tool("removable") is False
    assert not ex.has_tool("removable")


def test_executor_stats_available_and_unavailable_counts():
    """stats() must correctly report available vs unavailable tool counts."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _Ready(BaseTool):
        spec = ToolSpec(name="ready_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="ready_tool", content="ok")

    class _NotReady(BaseTool):
        spec = ToolSpec(name="not_ready_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="not_ready_tool", content="x")
        def available(self): return False

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id"

    ex = ToolExecutor({"ready_tool": _Ready(), "not_ready_tool": _NotReady()}, gate=_Gate())
    s = ex.stats()
    assert s["total"] == 2
    assert s["available"] == 1
    assert s["unavailable"] == 1


# --- Wave 3P additional tests ---------------------------------------------------

def test_executor_list_tools_returns_all_registered():
    """list_tools() returns the names of all registered tools."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _A(BaseTool):
        spec = ToolSpec(name="alpha", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="alpha", content="a")

    class _B(BaseTool):
        spec = ToolSpec(name="beta", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="beta", content="b")

    ex = ToolExecutor({"alpha": _A(), "beta": _B()})
    names = ex.list_tools()
    assert "alpha" in names and "beta" in names


def test_executor_tool_categories_includes_general():
    """tool_categories() includes 'general' as a default category."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="cat_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="cat_tool", content="x")

    ex = ToolExecutor({"cat_tool": _T()})
    cats = ex.tool_categories()
    assert "general" in cats


def test_redact_text_leaves_normal_text_unchanged():
    """redact_text() does not alter text without secrets."""
    from hive.core.redact import redact_text
    text = "The quick brown fox jumps over the lazy dog."
    assert redact_text(text) == text


def test_mask_secret_long_reveals_prefix_and_suffix():
    """mask_secret() for 20-char string shows 6-char prefix and 4-char suffix."""
    from hive.core.redact import mask_secret
    s = "ABCDEF" + "x" * 10 + "WXYZ"
    masked = mask_secret(s)
    assert masked.startswith("ABCDEF")
    assert masked.endswith("WXYZ")
    assert "…" in masked


def test_redact_value_leaves_int_unchanged():
    """redact_value() passes integers through unmodified."""
    from hive.core.redact import redact_value
    assert redact_value(42) == 42


def test_executor_dangerous_tools_empty_without_gate_recognizing_any():
    """dangerous_tools() returns an empty list when no tools are marked dangerous."""
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _Safe(BaseTool):
        spec = ToolSpec(name="safe_tool", description="d", parameters={}, dangerous=False)
        async def execute(self, **kw): return ToolResult(tool_name="safe_tool", content="ok")

    ex = ToolExecutor({"safe_tool": _Safe()})
    assert ex.dangerous_tools() == []


# --- Wave 3R: 6 new tests -------------------------------------------------------

def test_audit_log_search_by_tool_name():
    """search(tool=...) returns only entries for that tool."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "ping", "status": "ok", "args": {}})
    a.record({"tool": "pong", "status": "ok", "args": {}})
    results = a.search(tool="ping")
    assert len(results) == 1
    assert results[0]["tool"] == "ping"
    a.close()


def test_audit_log_search_by_status():
    """search(status=...) returns only entries matching that status."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "t1", "status": "ok", "args": {}})
    a.record({"tool": "t2", "status": "error", "args": {}})
    results = a.search(status="error")
    assert len(results) == 1
    assert results[0]["status"] == "error"
    a.close()


def test_audit_log_recent_errors_excludes_ok():
    """recent_errors() returns only entries with status != 'ok'."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "t", "status": "ok", "args": {}})
    a.record({"tool": "t", "status": "error", "args": {}})
    a.record({"tool": "t", "status": "timeout", "args": {}})
    errors = a.recent_errors()
    assert all(e["status"] != "ok" for e in errors)
    assert len(errors) == 2
    a.close()


def test_audit_log_purge_old_zero_days_removes_all():
    """purge_old(0) treats all entries as expired and removes them."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:")
    a.record({"tool": "x", "status": "ok", "args": {}})
    a.record({"tool": "y", "status": "ok", "args": {}})
    deleted = a.purge_old(0)
    assert deleted == 2
    assert a.count() == 0
    a.close()


def test_audit_log_prune_explicit_max_rows():
    """prune(max_rows=N) keeps only the N newest entries."""
    from hive.observability.audit import AuditLog
    a = AuditLog(":memory:", max_rows=1000)
    for i in range(5):
        a.record({"tool": f"t{i}", "status": "ok", "args": {}})
    deleted = a.prune(max_rows=2)
    assert deleted == 3
    assert a.count() == 2
    a.close()


def test_executor_execute_batch_returns_one_result_per_call():
    """execute_batch() returns a ToolDispatch for every (name, args) pair."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="bt", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="bt", content="ok")

    ex = ToolExecutor({"bt": _T()})
    results = asyncio.run(ex.execute_batch([("bt", {}), ("bt", {}), ("bt", {})]))
    assert len(results) == 3
    assert all(r.status is DispatchStatus.OK for r in results)
