"""
test_architecture.py — machine-enforce the dependency rule (SYNTHESIS A.3).

`hive.core.*` is the leaf layer: it must not import any higher layer
(llm/agents/memory/tools/context/gateway/autonomy/surfaces/observability). If this
rots, the "core stays plugin-agnostic" invariant is silently broken. This is the
lightweight analog of OpenClaw's check:import-cycles.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys

import hive.core as core_pkg

HIGHER_LAYERS = (
    "hive.llm", "hive.agents", "hive.memory", "hive.tools", "hive.context",
    "hive.gateway", "hive.autonomy", "hive.surfaces", "hive.observability",
)


def _core_modules() -> list[str]:
    return [f"hive.core.{m.name}" for m in pkgutil.iter_modules(core_pkg.__path__)]


def test_core_imports_no_higher_layer():
    for modname in _core_modules():
        importlib.import_module(modname)
    loaded = set(sys.modules)
    leaked = {
        m for m in loaded
        if any(m == h or m.startswith(h + ".") for h in HIGHER_LAYERS)
    }
    assert not leaked, f"core layer must not import higher layers, found: {sorted(leaked)}"


def test_core_layer_is_non_empty():
    # guard against the test silently passing because nothing was discovered
    assert _core_modules(), "no hive.core.* modules discovered"
