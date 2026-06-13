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
