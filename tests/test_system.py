"""P7 — system wiring: SystemBuilder.build() + system.ask() end-to-end (no network)."""
from __future__ import annotations

import asyncio
import json

from hive.core.config import HiveConfig, get_config
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.system import HiveSystem, SystemBuilder


class _ScriptRouter:
    """Stands in for ModelRouter; returns scripted results, records tool schemas seen."""
    def __init__(self, script):
        self._script = list(script)
        self.saw_tools = None
        self.closed = False

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        self.saw_tools = tools
        item = self._script.pop(0)
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="fake")

    async def aclose(self):
        self.closed = True


def _config(tmp_path) -> HiveConfig:
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_build_wires_all_subsystems_and_sets_global_config(tmp_path):
    cfg = _config(tmp_path)
    sys = SystemBuilder(cfg, router=_ScriptRouter([])).build()
    assert isinstance(sys, HiveSystem)
    # every subsystem wired
    for attr in ("events", "router", "tools", "tool_executor", "memory",
                 "session_store", "keeper", "planner", "orchestrator"):
        assert getattr(sys, attr) is not None
    assert "read_file" in sys.tools and "shell" in sys.tools
    # builder published the config to the global accessor (D1)
    assert get_config() is cfg
    # data dir was created
    assert cfg.data_dir.is_dir()


def test_ask_end_to_end_persists_and_recalls(tmp_path):
    router = _ScriptRouter([CompletionResult(text="hi Kamil", model="m")])
    sys = SystemBuilder(_config(tmp_path), router=router).build()

    answer = asyncio.run(sys.ask("hello", session_id="s1"))
    assert answer == "hi Kamil"
    # persisted to the session store...
    assert [m.content for m in sys.session_store.messages("s1")] == ["hello", "hi Kamil"]
    # ...and synced to memory (episodic recent)
    assert sys.memory.recent("s1")
    # tool schemas were advertised to the model (builtins present)
    assert router.saw_tools and any(t["name"] == "shell" for t in router.saw_tools)


def test_ask_runs_a_real_builtin_tool_end_to_end(tmp_path):
    target = tmp_path / "out.txt"
    call = ToolCall(id="c1", name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "from hive"}))
    router = _ScriptRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),  # ask for the tool
        CompletionResult(text="done", model="m"),                  # then answer
    ])
    sys = SystemBuilder(_config(tmp_path), router=router).build()
    answer = asyncio.run(sys.ask("please write the file"))
    assert answer == "done"
    assert target.read_text() == "from hive"          # the builtin actually ran


def test_aclose_closes_router(tmp_path):
    router = _ScriptRouter([])
    sys = SystemBuilder(_config(tmp_path), router=router).build()
    asyncio.run(sys.aclose())
    assert router.closed is True
