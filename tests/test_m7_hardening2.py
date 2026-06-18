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
