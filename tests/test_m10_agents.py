"""
test_m10_agents.py — M10-d: specialist sub-agents.

Tests:
  - All 5 agent YAML/MD files parse correctly (frontmatter + body present)
  - delegate_named resolves agents from the registry
  - get_agent_factory raises KeyError for unknown names
  - HiveOS.agents_registry contains all 5 specialist names after build
"""
from __future__ import annotations

import re
from pathlib import Path

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS

_AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
_EXPECTED_AGENTS = {
    "researcher", "coder", "reviewer", "memory-keeper", "security-reviewer",
}


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="[]", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# Agent file parsing
# ---------------------------------------------------------------------------

def test_agents_dir_exists():
    assert _AGENTS_DIR.is_dir(), f".claude/agents/ not found at {_AGENTS_DIR}"


def test_all_agent_files_present():
    found = {p.stem for p in _AGENTS_DIR.glob("*.md")}
    missing = _EXPECTED_AGENTS - found
    assert not missing, f"missing agent files: {missing}"


def test_agent_files_have_frontmatter():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert content.startswith("---"), f"{agent_file.name} missing YAML frontmatter"
        assert "---" in content[3:], f"{agent_file.name} frontmatter not closed"


def test_agent_files_have_name_field():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert re.search(r"^name:", content, re.MULTILINE), \
            f"{agent_file.name} missing 'name:' in frontmatter"


def test_agent_files_have_description():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        assert re.search(r"^description:", content, re.MULTILINE), \
            f"{agent_file.name} missing 'description:' in frontmatter"


def test_agent_files_have_body():
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text()
        parts = content.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else ""
        assert len(body) > 50, f"{agent_file.name} body is too short (< 50 chars)"


# ---------------------------------------------------------------------------
# Named registry — delegate module
# ---------------------------------------------------------------------------

def test_delegate_named_registry_resolves(tmp_path):
    from hive.agents.delegate import get_agent_factory
    _make_hive(tmp_path)  # populates _AGENT_REGISTRY as side-effect
    for name in _EXPECTED_AGENTS:
        factory = get_agent_factory(name)
        assert callable(factory), f"factory for {name!r} is not callable"


def test_get_agent_factory_raises_for_unknown():
    from hive.agents.delegate import get_agent_factory
    try:
        get_agent_factory("nonexistent_agent_xyz")
        assert False, "should have raised KeyError"
    except KeyError:
        pass


# ---------------------------------------------------------------------------
# HiveOS.agents_registry
# ---------------------------------------------------------------------------

def test_hive_agents_registry_populated(tmp_path):
    hive = _make_hive(tmp_path)
    assert hasattr(hive, "agents_registry")
    for name in _EXPECTED_AGENTS:
        assert name in hive.agents_registry, f"{name!r} missing from agents_registry"


def test_hive_agents_registry_values_callable(tmp_path):
    hive = _make_hive(tmp_path)
    for name, factory in hive.agents_registry.items():
        assert callable(factory), f"factory for {name!r} is not callable"


# --- Agent registry extra tests ---------------------------------------------------

def test_agent_frontmatter_model_field_valid():
    """Agent frontmatter model field, if present, should be a valid-looking model string."""
    import os, re
    agents_dir = os.path.join(os.path.dirname(__file__), "..", ".claude", "agents")
    agents_dir = os.path.realpath(agents_dir)
    for fname in os.listdir(agents_dir):
        if not fname.endswith(".md"):
            continue
        content = open(os.path.join(agents_dir, fname)).read()
        if "model:" in content[:500]:
            # If model field exists, it should look like a model string (not empty)
            m = re.search(r"^model:\s*(\S+)", content[:500], re.MULTILINE)
            if m:
                assert len(m.group(1)) > 3, f"{fname}: model field looks empty"


def test_all_agent_files_readable_utf8():
    """All .claude/agents/*.md files must be readable as UTF-8."""
    import os
    agents_dir = os.path.join(os.path.dirname(__file__), "..", ".claude", "agents")
    agents_dir = os.path.realpath(agents_dir)
    for fname in os.listdir(agents_dir):
        if fname.endswith(".md"):
            path = os.path.join(agents_dir, fname)
            open(path, encoding="utf-8").read()  # would raise on encoding error


def test_register_and_get_agent_factory():
    from hive.agents.delegate import register_agent, get_agent_factory

    def _factory():
        pass

    register_agent("test-extra-agent", _factory)
    assert get_agent_factory("test-extra-agent") is _factory


# ---------------------------------------------------------------------------
# New tests — frontmatter completeness, registry callable, delegate_named,
# per-name specialist presence, and name-filename alignment
# ---------------------------------------------------------------------------

def test_all_agents_have_description_field():
    """Every .claude/agents/*.md file must have a 'description:' key in its frontmatter."""
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text(encoding="utf-8")
        assert re.search(r"^description:", content, re.MULTILINE), (
            f"{agent_file.name} is missing 'description:' in frontmatter"
        )


def test_agent_factory_register_returns_callable():
    """A freshly registered factory must be retrieved as the exact same callable."""
    from hive.agents.delegate import register_agent, get_agent_factory

    def my_factory():
        return None

    register_agent("_test_callable_check", my_factory)
    retrieved = get_agent_factory("_test_callable_check")
    assert callable(retrieved), "retrieved factory is not callable"
    assert retrieved is my_factory, "retrieved factory is not the same object"


def test_delegate_named_returns_list(tmp_path):
    """delegate_named with a monkeypatched factory must return a list of AgentResult."""
    import asyncio
    from hive.agents.delegate import register_agent, delegate_named
    from hive.agents.base import AgentResult
    from hive.agents.executor import AgentExecutor, TerminalOutcome, TickResult

    class _FakeAgent:
        agent_id = "fake"
        accepts_tools = False

        async def run(self, input: str, context=None, **kw) -> AgentResult:
            return AgentResult(content=f"done:{input}")

    class _FakeExecutor(AgentExecutor):
        async def execute_tick(self, agent, task: str, context=None):  # type: ignore[override]
            result = await agent.run(task)
            return TickResult(
                outcome=TerminalOutcome.COMPLETED,
                result=result,
                error=None,
            )

    register_agent("_test_fake_agent", _FakeAgent)
    results = asyncio.run(
        delegate_named(["task1", "task2"], "_test_fake_agent",
                       executor=_FakeExecutor())
    )
    assert isinstance(results, list), "delegate_named did not return a list"
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    for r in results:
        assert isinstance(r, AgentResult)


def test_named_specialist_researcher_registered(tmp_path):
    """'researcher' must be present in the agent registry after HiveOS.build()."""
    hive = _make_hive(tmp_path)
    assert "researcher" in hive.agents_registry, \
        "'researcher' not found in agents_registry"
    assert callable(hive.agents_registry["researcher"])


def test_named_specialist_coder_registered(tmp_path):
    """'coder' must be present in the agent registry after HiveOS.build()."""
    hive = _make_hive(tmp_path)
    assert "coder" in hive.agents_registry, \
        "'coder' not found in agents_registry"
    assert callable(hive.agents_registry["coder"])


def test_agent_spec_name_matches_filename():
    """The 'name:' value in each agent's frontmatter must match the file stem."""
    for agent_file in _AGENTS_DIR.glob("*.md"):
        content = agent_file.read_text(encoding="utf-8")
        m = re.search(r"^name:\s*(\S+)", content, re.MULTILINE)
        assert m, f"{agent_file.name} has no 'name:' field"
        assert m.group(1) == agent_file.stem, (
            f"{agent_file.name}: frontmatter name={m.group(1)!r} "
            f"does not match stem={agent_file.stem!r}"
        )
