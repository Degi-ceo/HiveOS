"""M2 runtime integration — Curator + self-improvement wired into HiveOS + heartbeat."""
from __future__ import annotations

import asyncio

from hive.core.config import HiveConfig
from hive.core.events import EventType
from hive.core.spec_search import Edit, EditOp, RiskTier
from hive.llm.adapters.base import CompletionResult
from hive.memory.skill_usage import STATE_ACTIVE
from hive.runtime import HiveOS


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="ok", model="fake")

    async def aclose(self):
        pass


def _hive(tmp_path) -> HiveOS:
    return HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_Router())


def test_build_wires_m2_subsystems(tmp_path):
    h = _hive(tmp_path)
    for attr in ("skill_usage", "curator", "self_modifier", "improver"):
        assert getattr(h, attr) is not None


def test_builtin_tools_registered_as_non_agent_created(tmp_path):
    # Built-ins must be exempt from the Curator lifecycle (never archived).
    h = _hive(tmp_path)
    for name in h.tools:
        u = h.skill_usage.get(name)
        assert u is not None and u.agent_created is False


def test_tool_use_records_skill_usage(tmp_path):
    h = _hive(tmp_path)
    name = next(iter(h.tools))
    before = h.skill_usage.get(name).use_count
    # Simulate a successful tool call event on the bus.
    h.events.publish(EventType.TOOL_CALL_END, {"tool": name, "status": "ok"})
    assert h.skill_usage.get(name).use_count == before + 1


def test_tool_error_does_not_record_use(tmp_path):
    h = _hive(tmp_path)
    name = next(iter(h.tools))
    before = h.skill_usage.get(name).use_count
    h.events.publish(EventType.TOOL_CALL_END, {"tool": name, "status": "error"})
    assert h.skill_usage.get(name).use_count == before


def test_curate_is_noop_with_only_builtins(tmp_path):
    h = _hive(tmp_path)
    report = h.curate()
    # Built-ins are non-agent-created -> never transitioned.
    assert report["transitions"] == []


def test_self_improve_manual_and_review_paths(tmp_path):
    h = _hive(tmp_path)

    async def _noop(_wt):
        return ["src/hive/x.py"]

    edits = [
        Edit(op=EditOp.DEPENDENCY_CHANGE, summary="bump", apply=_noop),   # MANUAL
        Edit(op=EditOp.PATCH_CODE, summary="patch", apply=_noop),         # REVIEW -> gate
    ]
    outs = asyncio.run(h.self_improve(edits))
    by_op = {o.op: o for o in outs}
    assert by_op[EditOp.DEPENDENCY_CHANGE].status == "manual"
    assert by_op[EditOp.PATCH_CODE].status == "pending_approval"
    assert by_op[EditOp.PATCH_CODE].approval_id is not None


def test_heartbeat_reports_curated(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    h = _hive(tmp_path)
    hb = Heartbeat(h, goals=["stay healthy"])
    summary = asyncio.run(hb.tick())
    assert "curated" in summary and summary["curated"] == 0


def test_aclose_closes_skill_usage(tmp_path):
    h = _hive(tmp_path)
    asyncio.run(h.aclose())
    # closed connection raises on use — proves close() ran
    import pytest
    with pytest.raises(Exception):
        h.skill_usage.all()
