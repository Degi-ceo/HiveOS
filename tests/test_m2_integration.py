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


# --- Extra M2 integration tests --------------------------------------------------

def test_skill_usage_tracks_new_tool_after_build(tmp_path):
    h = _hive(tmp_path)
    # All built-in tools should be registered in skill_usage
    for name in h.tools:
        u = h.skill_usage.get(name)
        assert u is not None, f"skill_usage missing entry for {name}"


def test_events_bus_wired_in_hive(tmp_path):
    from hive.core.events import EventType
    h = _hive(tmp_path)
    received = []
    h.events.subscribe(EventType.TOOL_CALL_START, lambda e: received.append(e))
    h.events.publish(EventType.TOOL_CALL_START, {"tool": "test"})
    assert len(received) == 1
    assert received[0].data["tool"] == "test"


def test_self_improve_auto_tier_returns_one_result(tmp_path, monkeypatch):
    h = _hive(tmp_path)
    proposed = []

    async def _fake_propose(edit, *, wt=None):
        proposed.append(edit)
        return {"ok": True, "stage": "proposed"}

    monkeypatch.setattr(h.self_modifier, "propose", _fake_propose)

    async def _noop_apply(_wt):
        return ["src/hive/docs/x.md"]

    edits = [Edit(op=EditOp.EDIT_DOCS, summary="docs update", apply=_noop_apply)]
    outs = asyncio.run(h.self_improve(edits))
    assert len(outs) == 1


def test_curate_umbrella_returns_dict(tmp_path):
    h = _hive(tmp_path)
    result = asyncio.run(h.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result


# --- Extra M2 integration tests --------------------------------------------------

def test_skill_usage_tracks_new_tool_after_build(tmp_path):
    h = _hive(tmp_path)
    for name in h.tools:
        u = h.skill_usage.get(name)
        assert u is not None, f"skill_usage missing entry for {name}"


def test_events_bus_wired_in_hive(tmp_path):
    from hive.core.events import EventType
    h = _hive(tmp_path)
    received = []
    h.events.subscribe(EventType.TOOL_CALL_START, lambda e: received.append(e))
    h.events.publish(EventType.TOOL_CALL_START, {"tool": "test"})
    assert len(received) == 1
    assert received[0].data["tool"] == "test"


def test_self_improve_auto_tier_returns_one_result(tmp_path, monkeypatch):
    h = _hive(tmp_path)
    proposed = []

    async def _fake_propose(summary, rationale, apply_fn, *, dry_run=False, wt=None):
        proposed.append(summary)
        return {"ok": True, "stage": "proposed"}

    monkeypatch.setattr(h.self_modifier, "propose", _fake_propose)

    async def _noop_apply(_wt):
        return ["src/hive/docs/x.md"]

    edits = [Edit(op=EditOp.EDIT_DOCS, summary="docs update", apply=_noop_apply)]
    outs = asyncio.run(h.self_improve(edits))
    assert len(outs) == 1


def test_curate_umbrella_returns_dict(tmp_path):
    h = _hive(tmp_path)
    result = asyncio.run(h.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result


# --- Additional M2 integration tests (unique names) ----------------------------

def test_hiveos_router_attribute_is_set_after_build(tmp_path):
    """HiveOS.router must be the object passed into build()."""
    router = _Router()
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=router)
    assert h.router is router


def test_hiveos_skill_usage_not_none_after_build(tmp_path):
    """skill_usage must be initialised — never None — after build()."""
    h = _hive(tmp_path)
    assert h.skill_usage is not None


def test_curator_transitions_attribute_accessible(tmp_path):
    """curator.run() always returns a dict with a 'transitions' list."""
    h = _hive(tmp_path)
    report = h.curate()
    assert "transitions" in report
    assert isinstance(report["transitions"], list)


def test_heartbeat_tick_returns_expected_keys(tmp_path):
    """Heartbeat.tick() must return a dict with the standard metrics keys."""
    from hive.autonomy.heartbeat import Heartbeat
    h = _hive(tmp_path)
    hb = Heartbeat(h, goals=["test"])
    summary = asyncio.run(hb.tick())
    for key in ("cron", "commitments", "planned", "dispatched", "consolidated", "curated"):
        assert key in summary, f"missing key: {key}"


def test_self_improve_edit_docs_tier_returns_auto_stage_result(tmp_path, monkeypatch):
    """EDIT_DOCS is AUTO-tier; self_improve must return exactly one outcome with status 'applied'."""
    h = _hive(tmp_path)

    async def _fake_propose(summary, rationale, apply_fn, *, dry_run=False, wt=None):
        return {"ok": True, "stage": "applied", "branch": "draft/docs-update"}

    monkeypatch.setattr(h.self_modifier, "propose", _fake_propose)

    async def _noop_apply(_wt):
        return ["src/hive/docs/example.md"]

    edits = [Edit(op=EditOp.EDIT_DOCS, summary="update docs", apply=_noop_apply)]
    outs = asyncio.run(h.self_improve(edits))
    assert len(outs) == 1
    outcome = outs[0]
    assert outcome.status == "applied"
    assert outcome.op == EditOp.EDIT_DOCS


def test_self_improve_add_test_tier_is_auto(tmp_path):
    """ADD_TEST edits must be classified as AUTO tier (deterministic table check)."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.ADD_TEST) is RiskTier.AUTO


def test_hiveos_health_returns_status_ok(tmp_path):
    """HiveOS.health() must include status=ok and standard top-level keys."""
    h = _hive(tmp_path)
    snap = h.health()
    assert snap.get("status") == "ok"
    assert "tools" in snap
    assert "budget" in snap


# --- Wave 3K additional tests -------------------------------------------------

def test_hive_audit_log_attribute_exists(tmp_path):
    """HiveOS.audit_log is accessible after build."""
    h = _hive(tmp_path)
    assert h.audit_log is not None


def test_hive_telemetry_attribute_exists(tmp_path):
    """HiveOS.telemetry is set and non-None."""
    h = _hive(tmp_path)
    assert h.telemetry is not None


def test_hive_budgeter_attribute_exists(tmp_path):
    """HiveOS.budgeter is accessible."""
    h = _hive(tmp_path)
    assert h.budgeter is not None


def test_hive_loop_guard_attribute_exists(tmp_path):
    """HiveOS.loop_guard is accessible."""
    h = _hive(tmp_path)
    assert h.loop_guard is not None


def test_hive_system_status_memory_key(tmp_path):
    """system_status() result includes a 'memory' key."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert "memory" in status


def test_hive_orchestrator_attribute_exists(tmp_path):
    """HiveOS.orchestrator is set after build."""
    h = _hive(tmp_path)
    assert h.orchestrator is not None


# --- Wave 4 additional tests (6) -----------------------------------------------

def test_system_status_has_tasks_key(tmp_path):
    """system_status() must include a 'tasks' key."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert "tasks" in status


def test_system_status_has_pending_approvals_key(tmp_path):
    """system_status() must include a 'pending_approvals' key."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert "pending_approvals" in status


def test_health_has_telemetry_key(tmp_path):
    """health() must include a 'telemetry' key."""
    h = _hive(tmp_path)
    snap = h.health()
    assert "telemetry" in snap


def test_health_has_pending_review_edits_key(tmp_path):
    """health() must include a 'pending_review_edits' key."""
    h = _hive(tmp_path)
    snap = h.health()
    assert "pending_review_edits" in snap


def test_loop_guard_stats_returns_expected_keys(tmp_path):
    """loop_guard_stats() must return a dict with 'total_calls' and 'per_tool' keys."""
    h = _hive(tmp_path)
    stats = h.loop_guard_stats()
    assert isinstance(stats, dict)
    assert "total_calls" in stats
    assert "per_tool" in stats


def test_system_status_cron_jobs_is_int(tmp_path):
    """system_status()['cron_jobs'] must be an integer."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert isinstance(status["cron_jobs"], int)


# --- Wave 5 additional tests (6) -----------------------------------------------

def test_system_status_tools_is_dict(tmp_path):
    """system_status()['tools'] must be a dict with 'total' and 'available' keys."""
    h = _hive(tmp_path)
    status = h.system_status()
    tools = status["tools"]
    assert isinstance(tools, dict)
    assert "total" in tools
    assert "available" in tools


def test_system_status_tools_total_positive(tmp_path):
    """system_status()['tools']['total'] must be a positive integer."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert isinstance(status["tools"]["total"], int)
    assert status["tools"]["total"] > 0


def test_health_budget_has_daily_cap(tmp_path):
    """health()['budget'] must include a 'daily_cap' key with a positive value."""
    h = _hive(tmp_path)
    snap = h.health()
    budget = snap["budget"]
    assert "daily_cap" in budget
    assert budget["daily_cap"] > 0


def test_budgeter_gate_allows_when_under_cap(tmp_path):
    """budgeter.gate() must return (True, '') when no calls have been made."""
    h = _hive(tmp_path)
    allowed, msg = h.budgeter.gate()
    assert allowed is True
    assert isinstance(msg, str)


def test_reset_loop_guard_clears_stats(tmp_path):
    """loop_guard_stats()['total_calls'] must be 0 after reset_loop_guard()."""
    h = _hive(tmp_path)
    h.reset_loop_guard()
    stats = h.loop_guard_stats()
    assert stats["total_calls"] == 0


def test_health_self_mod_proposals_is_int(tmp_path):
    """health()['self_mod_proposals'] must be an integer >= 0."""
    h = _hive(tmp_path)
    snap = h.health()
    assert "self_mod_proposals" in snap
    assert isinstance(snap["self_mod_proposals"], int)
    assert snap["self_mod_proposals"] >= 0


# --- Wave 3V-A: 8 new M2 integration tests --------------------------------------

def test_wave3v_assign_tier_patch_code_is_review(tmp_path):
    """PATCH_CODE must be classified as REVIEW tier."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.PATCH_CODE) is RiskTier.REVIEW


def test_wave3v_assign_tier_create_file_is_auto(tmp_path):
    """CREATE_FILE must be classified as AUTO tier."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.CREATE_FILE) is RiskTier.AUTO


def test_wave3v_assign_tier_infra_deploy_is_manual(tmp_path):
    """INFRA_DEPLOY must be classified as MANUAL tier."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.INFRA_DEPLOY) is RiskTier.MANUAL


def test_wave3v_tool_call_end_ok_increments_use_count(tmp_path):
    """TOOL_CALL_END with status='ok' increments use_count by 1."""
    from hive.core.events import EventType
    h = _hive(tmp_path)
    name = next(iter(h.tools))
    before = h.skill_usage.get(name).use_count
    h.events.publish(EventType.TOOL_CALL_END, {"tool": name, "status": "ok"})
    h.events.publish(EventType.TOOL_CALL_END, {"tool": name, "status": "ok"})
    assert h.skill_usage.get(name).use_count == before + 2


def test_wave3v_system_status_self_mod_history_is_int(tmp_path):
    """system_status()['self_mod_history_count'] must be a non-negative integer."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert "self_mod_history_count" in status
    assert isinstance(status["self_mod_history_count"], int)
    assert status["self_mod_history_count"] >= 0


def test_wave3v_system_status_active_commitments_is_int(tmp_path):
    """system_status()['active_commitments'] must be a non-negative integer."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert "active_commitments" in status
    assert isinstance(status["active_commitments"], int)
    assert status["active_commitments"] >= 0


def test_wave3v_health_active_commitments_key_present(tmp_path):
    """health() must include an 'active_commitments' key."""
    h = _hive(tmp_path)
    snap = h.health()
    assert "active_commitments" in snap
    assert isinstance(snap["active_commitments"], int)


def test_wave3v_abort_all_self_mods_returns_zero_when_none_pending(tmp_path):
    """abort_all_self_mods() must return 0 when no REVIEW-tier edits are pending."""
    h = _hive(tmp_path)
    count = h.abort_all_self_mods()
    assert count == 0


# --- Wave 3Z-A additional tests (8) --------------------------------------------

def test_wave3z_tools_count_above_minimum(tmp_path):
    h = _hive(tmp_path)
    assert len(h.tools) >= 5


def test_wave3z_memory_not_none(tmp_path):
    h = _hive(tmp_path)
    assert h.memory is not None


def test_wave3z_system_status_has_router_key(tmp_path):
    h = _hive(tmp_path)
    status = h.system_status()
    assert isinstance(status, dict)
    assert "router" in status


def test_wave3z_tool_names_are_strings(tmp_path):
    h = _hive(tmp_path)
    for name in h.tools:
        assert isinstance(name, str), f"tool name {name!r} is not a str"


def test_wave3z_specialist_agents_have_run_method(tmp_path):
    h = _hive(tmp_path)
    for name, factory in h.agents_registry.items():
        agent = factory()
        assert callable(getattr(agent, "run", None)), f"{name}: agent.run is not callable"


def test_wave3z_traces_total_event_count_is_int(tmp_path):
    h = _hive(tmp_path)
    count = h.traces.total_event_count()
    assert isinstance(count, int)
    assert count >= 0


def test_wave3z_telemetry_inference_calls_is_int(tmp_path):
    h = _hive(tmp_path)
    assert isinstance(h.telemetry.inference_calls, int)
    assert h.telemetry.inference_calls >= 0


def test_wave3z_task_board_pending_count_zero_initially(tmp_path):
    h = _hive(tmp_path)
    assert h.task_board.pending_count() == 0


# --- Wave 4D-B additional tests (8) -------------------------------------------

def test_wave4d_traces_not_none(tmp_path):
    h = _hive(tmp_path)
    assert h.traces is not None


def test_wave4d_task_board_not_none(tmp_path):
    h = _hive(tmp_path)
    assert h.task_board is not None


def test_wave4d_shell_tool_is_registered(tmp_path):
    h = _hive(tmp_path)
    assert "shell" in h.tools


def test_wave4d_web_get_tool_is_registered(tmp_path):
    h = _hive(tmp_path)
    assert "web_get" in h.tools


def test_wave4d_system_status_budget_key_present(tmp_path):
    h = _hive(tmp_path)
    status = h.system_status()
    assert "budget" in status


def test_wave4d_system_status_tools_by_category_is_dict(tmp_path):
    h = _hive(tmp_path)
    status = h.system_status()
    by_cat = status["tools"]["by_category"]
    assert isinstance(by_cat, dict)
    assert len(by_cat) > 0


def test_wave4d_host_llm_attribute_present(tmp_path):
    h = _hive(tmp_path)
    assert h.host_llm is not None


def test_wave4d_traces_total_event_count_non_negative(tmp_path):
    h = _hive(tmp_path)
    count = h.traces.total_event_count()
    assert isinstance(count, int) and count >= 0


# --- Wave 4J additional tests (8) ---------------------------------------------

def test_wave4j_discover_tool_registered(tmp_path):
    h = _hive(tmp_path)
    assert "discover" in h.tools


def test_wave4j_delete_file_tool_registered(tmp_path):
    h = _hive(tmp_path)
    assert "delete_file" in h.tools


def test_wave4j_write_file_tool_registered(tmp_path):
    h = _hive(tmp_path)
    assert "write_file" in h.tools


def test_wave4j_memory_name_attribute(tmp_path):
    h = _hive(tmp_path)
    assert hasattr(h.memory, "name")
    assert isinstance(h.memory.name, str)


def test_wave4j_system_status_memory_key(tmp_path):
    h = _hive(tmp_path)
    status = h.system_status()
    assert "memory" in status


def test_wave4j_audit_log_accessible(tmp_path):
    h = _hive(tmp_path)
    assert h.audit_log is not None
    count = h.audit_log.count()
    assert isinstance(count, int) and count >= 0


def test_wave4j_researcher_agent_in_registry(tmp_path):
    h = _hive(tmp_path)
    assert "researcher" in h.agents_registry


def test_wave4j_security_reviewer_agent_in_registry(tmp_path):
    h = _hive(tmp_path)
    assert "security-reviewer" in h.agents_registry


# --- Wave 4Q additional integration tests (8) ----------------------------------

def test_wave4q_read_file_tool_registered(tmp_path):
    """'read_file' tool must be present in h.tools after build."""
    h = _hive(tmp_path)
    assert "read_file" in h.tools


def test_wave4q_shell_tool_registered(tmp_path):
    """'shell' tool must be present in h.tools after build."""
    h = _hive(tmp_path)
    assert "shell" in h.tools


def test_wave4q_task_board_not_none(tmp_path):
    """h.task_board must not be None after build."""
    h = _hive(tmp_path)
    assert h.task_board is not None


def test_wave4q_router_not_none(tmp_path):
    """h.router must not be None after build."""
    h = _hive(tmp_path)
    assert h.router is not None


def test_wave4q_system_status_has_router_key(tmp_path):
    """system_status() must return a dict containing a 'router' key."""
    h = _hive(tmp_path)
    status = h.system_status()
    assert isinstance(status, dict)
    assert "router" in status


def test_wave4q_skill_usage_register_shows_in_by_state(tmp_path):
    """Registering a new skill via skill_usage.register() shows it in by_state('active')."""
    h = _hive(tmp_path)
    h.skill_usage.register("wave4q_test_skill", agent_created=True)
    active_names = [s.name for s in h.skill_usage.by_state("active")]
    assert "wave4q_test_skill" in active_names


def test_wave4q_health_status_ok(tmp_path):
    """health()['status'] must equal 'ok'."""
    h = _hive(tmp_path)
    snap = h.health()
    assert snap["status"] == "ok"


def test_wave4q_coder_agent_in_registry(tmp_path):
    """'coder' must be present in h.agents_registry after build."""
    h = _hive(tmp_path)
    assert "coder" in h.agents_registry
