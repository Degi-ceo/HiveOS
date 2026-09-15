"""M9 foundation: enforceable specialist profiles and durable delegation state."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import socket
import sqlite3

import pytest

from hive.agents.base import AgentResult, BaseAgent
from hive.agents.delegate import register_agent
from hive.agents.delegations import CANCELLED, COMPLETED, FAILED, REVIEW_REQUIRED, RUNNING, DelegationLedger
from hive.agents.profiles import scoped_specialist_tools, specialist_profile, specialist_profiles
from hive.core.config import HiveConfig
from hive.core.events import EventBus
from hive.core.run_context import bind_run_id
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.builtins import DelegateToSpecialist


def test_profiles_are_closed_and_coder_requires_independent_review():
    assert {profile.name for profile in specialist_profiles()} == {
        "researcher", "reviewer", "security-reviewer", "memory-keeper", "coder",
    }
    assert specialist_profile("coder").requires_independent_review
    with pytest.raises(ValueError, match="unknown specialist role"):
        specialist_profile("release-manager")


def test_profile_tool_snapshots_are_fail_closed():
    tools = {name: object() for name in {
        "read_file", "shell", "write_file", "delegate_to_specialist", "new_mcp_tool",
    }}
    researcher = scoped_specialist_tools("researcher", tools)
    coder = scoped_specialist_tools("coder", tools)
    assert set(researcher) == {"read_file"}
    assert set(coder) == {"read_file"}
    assert "delegate_to_specialist" not in researcher
    assert "new_mcp_tool" not in coder


def test_ledger_persists_and_fences_terminal_result(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    attempt = ledger.claim(item.id)
    assert attempt == 1
    assert ledger.get(item.id).state == RUNNING
    assert ledger.finish(item.id, attempt=attempt, success=True, summary="token=secret")
    completed = ledger.get(item.id)
    assert completed.state == COMPLETED
    assert "secret" not in completed.safe_summary
    assert not ledger.finish(item.id, attempt=attempt, success=False, summary="late")
    ledger.close()


def test_ledger_failure_is_durable_and_parent_query_is_bounded(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="reviewer")
    attempt = ledger.claim(item.id)
    assert ledger.finish(item.id, attempt=attempt, success=False, summary="failed")
    assert ledger.get(item.id).state == FAILED
    assert [row.id for row in ledger.for_parent("parent")] == [item.id]
    ledger.close()


def test_ledger_recovers_only_dead_work_owned_by_this_host(tmp_path):
    db_path = tmp_path / "state.sqlite"
    owner = DelegationLedger(
        db_path, hostname="local-host", process_id=123,
        process_is_alive=lambda _pid: False,
    )
    item = owner.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert owner.claim(item.id) == 1
    owner.close()

    restarted = DelegationLedger(
        db_path, hostname="local-host", process_id=456,
        process_is_alive=lambda _pid: False,
    )
    assert restarted.recover_interrupted() == 1
    recovered = restarted.get(item.id)
    assert recovered.state == FAILED
    assert recovered.safe_summary == "worker interrupted; recovery requires replanning"
    restarted.close()


def test_ledger_never_recovers_live_or_remote_work(tmp_path):
    db_path = tmp_path / "state.sqlite"
    remote = DelegationLedger(
        db_path, hostname="remote-host", process_id=123,
        process_is_alive=lambda _pid: False,
    )
    remote_item = remote.create(parent_run_id="parent", child_run_id="remote", role="researcher")
    assert remote.claim(remote_item.id) == 1
    remote.close()

    live_local = DelegationLedger(
        db_path, hostname="local-host", process_id=456,
        process_is_alive=lambda _pid: True,
    )
    local_item = live_local.create(parent_run_id="parent", child_run_id="local", role="researcher")
    assert live_local.claim(local_item.id) == 1
    assert live_local.recover_interrupted() == 0
    assert live_local.get(remote_item.id).state == RUNNING
    assert live_local.get(local_item.id).state == RUNNING
    live_local.close()


def test_ledger_never_recovers_same_hostname_from_another_machine(tmp_path):
    db_path = tmp_path / "state.sqlite"
    remote = DelegationLedger(
        db_path, hostname="shared-name", machine_identity="remote-machine", process_id=123,
        process_is_alive=lambda _pid: False,
    )
    item = remote.create(parent_run_id="parent", child_run_id="remote", role="researcher")
    assert remote.claim(item.id) == 1
    remote.close()

    local = DelegationLedger(
        db_path, hostname="shared-name", machine_identity="local-machine", process_id=456,
        process_is_alive=lambda _pid: False,
    )
    assert local.recover_interrupted() == 0
    assert local.get(item.id).state == RUNNING
    local.close()


def test_ledger_recovers_known_hive_pid_reuse_without_trusting_pid_alone(tmp_path):
    db_path = tmp_path / "state.sqlite"
    crashed = DelegationLedger(
        db_path, hostname="local-host", machine_identity="machine", process_id=123,
        process_is_alive=lambda _pid: True,
    )
    item = crashed.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert crashed.claim(item.id) == 1

    replacement = DelegationLedger(
        db_path, hostname="local-host", machine_identity="machine", process_id=123,
        process_is_alive=lambda _pid: True,
    )
    assert replacement.recover_interrupted() == 1
    assert replacement.get(item.id).state == FAILED
    replacement.close()
    crashed.close()


def test_ledger_leaves_an_unidentified_live_reused_pid_untouched(tmp_path):
    db_path = tmp_path / "state.sqlite"
    prior = DelegationLedger(
        db_path, hostname="local-host", machine_identity="machine", process_id=123,
        process_is_alive=lambda _pid: True,
    )
    item = prior.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert prior.claim(item.id) == 1
    prior.close()

    restarted = DelegationLedger(
        db_path, hostname="local-host", machine_identity="machine", process_id=456,
        process_is_alive=lambda _pid: True,
    )
    assert restarted.recover_interrupted() == 0
    assert restarted.get(item.id).state == RUNNING
    restarted.close()


def test_ledger_claim_is_atomic_across_sqlite_connections(tmp_path):
    db_path = tmp_path / "state.sqlite"
    first = DelegationLedger(db_path, hostname="first", process_id=1)
    item = first.create(parent_run_id="parent", child_run_id="child", role="researcher")
    second = DelegationLedger(db_path, hostname="second", process_id=2)
    barrier = threading.Barrier(2)

    def claim(ledger):
        barrier.wait()
        return ledger.claim(item.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))
    assert results.count(1) == 1
    assert results.count(None) == 1
    assert first.get(item.id).state == RUNNING
    first.close()
    second.close()


def test_ledger_migrates_existing_schema_before_claiming(tmp_path):
    db_path = tmp_path / "state.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE hive_delegations(
          id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL, child_run_id TEXT NOT NULL,
          role TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
          safe_summary TEXT NOT NULL DEFAULT '');
        CREATE TABLE hive_delegation_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, delegation_id TEXT NOT NULL,
          ts REAL NOT NULL, event_type TEXT NOT NULL, data_json TEXT NOT NULL);
    """)
    db.commit()
    db.close()

    ledger = DelegationLedger(db_path, hostname="local-host", process_id=123)
    item = ledger.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert ledger.claim(item.id) == 1
    claimed = ledger.get(item.id)
    assert claimed.owner_host == "local-host"
    assert claimed.owner_pid == 123
    ledger.close()


def test_ledger_migrates_legacy_schema_concurrently(tmp_path):
    db_path = tmp_path / "state.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE hive_delegations(
          id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL, child_run_id TEXT NOT NULL,
          role TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
          safe_summary TEXT NOT NULL DEFAULT '');
    """)
    db.close()
    barrier = threading.Barrier(2)

    def open_ledger(process_id):
        barrier.wait()
        ledger = DelegationLedger(db_path, hostname="local-host", machine_identity="machine",
                                  process_id=process_id)
        ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_ledger, (1, 2)))

    db = sqlite3.connect(db_path)
    columns = {row[1] for row in db.execute("PRAGMA table_info(hive_delegations)")}
    assert {"owner_host", "owner_pid", "owner_machine_id", "owner_instance_id"} <= columns
    db.close()


def test_runtime_build_recovers_interrupted_local_delegation(tmp_path, monkeypatch):
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kwargs: None)
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    prior = DelegationLedger(config.state_db, hostname=socket.gethostname(), process_id=0)
    item = prior.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert prior.claim(item.id) == 1
    prior.close()

    hive = HiveOS.build(config, router=_Router())
    assert hive.delegation_ledger.get(item.id).state == FAILED
    asyncio.run(hive.aclose())


def test_runtime_resume_after_restart_reports_interrupted_delegation(tmp_path, monkeypatch):
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kwargs: None)
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(config, router=_Router())
    prior = DelegationLedger(config.state_db, hostname=socket.gethostname(), process_id=0)
    item = prior.create(parent_run_id="parent", child_run_id="child", role="researcher")
    assert prior.claim(item.id) == 1
    prior.close()

    assert hive.resume_after_restart()["interrupted_delegations"] == 1
    assert hive.delegation_ledger.get(item.id).state == FAILED
    asyncio.run(hive.aclose())


def test_real_delegate_creates_and_finishes_durable_record(tmp_path):
    class _Leaf(BaseAgent):
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="done")

    register_agent("researcher", lambda: _Leaf())
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    tool = DelegateToSpecialist(bus=EventBus(), delegation_ledger=ledger)

    async def run():
        with bind_run_id("parent"):
            return await tool.execute(agent="researcher", task="safe research")

    assert asyncio.run(run()).content == "done"
    rows = ledger.for_parent("parent")
    assert len(rows) == 1
    assert rows[0].role == "researcher"
    assert rows[0].state == COMPLETED
    ledger.close()


def test_coder_output_requires_independent_review(tmp_path):
    class _Leaf(BaseAgent):
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="unreviewed edit")

    register_agent("coder", lambda: _Leaf())
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    bus = EventBus()
    completed = []
    from hive.core.events import EventType
    bus.subscribe(EventType.A2A_CALL_COMPLETED, completed.append)
    result = asyncio.run(DelegateToSpecialist(bus=bus, delegation_ledger=ledger).execute(
        agent="coder", task="draft a change",
    ))
    row = ledger.for_parent("")[0]
    assert not result.success
    assert result.content == "[delegate review required]"
    assert row.state == REVIEW_REQUIRED
    assert completed and completed[0].data["result"] == "[delegate review required]"
    assert "unreviewed edit" not in str(completed)
    ledger.close()


def test_coder_review_boundary_applies_without_a_ledger():
    class _Leaf(BaseAgent):
        async def run(self, input, context=None, **kwargs):
            return AgentResult(content="unreviewed direct result")

    register_agent("coder", lambda: _Leaf())
    bus = EventBus()
    completed = []
    from hive.core.events import EventType
    bus.subscribe(EventType.A2A_CALL_COMPLETED, completed.append)
    result = asyncio.run(DelegateToSpecialist(bus=bus).execute(
        agent="coder", task="draft a change",
    ))
    assert not result.success
    assert result.content == "[delegate review required]"
    assert completed and completed[0].data["result"] == "[delegate review required]"
    assert "unreviewed direct result" not in str(completed)


def test_cancelled_delegate_is_terminal_and_fenced(tmp_path, monkeypatch):
    async def _blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("hive.agents.delegate.delegate_via_envelope", _blocked)
    ledger = DelegationLedger(tmp_path / "state.sqlite")

    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                DelegateToSpecialist(delegation_ledger=ledger).execute(
                    agent="researcher", task="wait",
                ), timeout=0.01,
            )

    asyncio.run(run())
    row = ledger.for_parent("")[0]
    assert row.state == CANCELLED
    assert not ledger.finish(row.id, attempt=1, success=True, summary="late")
    ledger.close()


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kwargs):
        return CompletionResult(text="ok", model="fake")

    async def aclose(self):
        pass


def test_runtime_leaf_agents_receive_only_profiled_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kwargs: None)
    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_Router())
    researcher = hive.agents_registry["researcher"]()
    coder = hive.agents_registry["coder"]()
    assert "write_file" not in researcher._tools
    assert "shell" not in researcher._tools
    assert "delegate_to_specialist" not in researcher._tools
    assert "write_file" not in coder._tools
    assert "shell" not in coder._tools
    assert "delegate_to_specialist" not in coder._tools
    asyncio.run(hive.aclose())


def test_unknown_role_is_rejected_before_ledger_write(tmp_path):
    ledger = DelegationLedger(tmp_path / "state.sqlite")
    result = asyncio.run(DelegateToSpecialist(delegation_ledger=ledger).execute(
        agent="release-manager", task="do not run",
    ))
    assert not result.success
    assert "unknown specialist role" in result.content
    assert ledger.for_parent("") == []
    ledger.close()
