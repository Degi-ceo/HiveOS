"""
test_architecture.py — machine-enforce the dependency rule (SYNTHESIS A.3).

`hive.core.*` is the leaf layer: it must not import any higher layer
(llm/agents/memory/tools/context/gateway/autonomy/surfaces/observability). If this
rots, the "core stays plugin-agnostic" invariant is silently broken. This is the
lightweight analog of OpenClaw's check:import-cycles.

The check runs in a FRESH subprocess so sys.modules reflects only what importing
hive.core transitively pulls in — not modules other tests already loaded.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import hive

SRC_DIR = str(pathlib.Path(hive.__file__).resolve().parents[1])

_PROBE = textwrap.dedent(
    """
    import importlib, json, pkgutil, sys
    import hive.core as core
    mods = [f"hive.core.{m.name}" for m in pkgutil.iter_modules(core.__path__)]
    for m in mods:
        importlib.import_module(m)
    higher = ("hive.llm", "hive.agents", "hive.memory", "hive.tools", "hive.context",
              "hive.gateway", "hive.autonomy", "hive.surfaces", "hive.observability")
    leaked = sorted(m for m in sys.modules
                    if any(m == h or m.startswith(h + ".") for h in higher))
    print(json.dumps({"mods": mods, "leaked": leaked}))
    """
)


def _probe() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, env={"PYTHONPATH": SRC_DIR, "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_core_imports_no_higher_layer():
    data = _probe()
    assert data["mods"], "no hive.core.* modules discovered"
    assert not data["leaked"], f"core layer must not import higher layers: {data['leaked']}"
