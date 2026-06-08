"""
test_architecture.py — machine-enforce the dependency DAG (SYNTHESIS A.3).

Layers may only import downward. Two invariants enforced here:
  - `hive.core.*` is the leaf: it imports no higher layer.
  - `hive.memory.*` depends on `hive.core` ONLY (notably NOT `hive.llm`): the
    memory-keeper takes an injected Summarizer instead of importing the router.
If this rots, the "core stays plugin-agnostic" / strict-DAG invariant is silently
broken. Lightweight analog of OpenClaw's check:import-cycles.

Each check runs in a FRESH subprocess so sys.modules reflects only what importing
that package transitively pulls in — not modules other tests already loaded.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

import hive

SRC_DIR = str(pathlib.Path(hive.__file__).resolve().parents[1])

_ALL_LAYERS = ("hive.core", "hive.llm", "hive.agents", "hive.memory", "hive.tools",
               "hive.context", "hive.gateway", "hive.autonomy", "hive.surfaces",
               "hive.observability")


def _probe(package: str, allowed: tuple[str, ...]) -> dict:
    forbidden = [layer for layer in _ALL_LAYERS if layer not in allowed]
    code = (
        "import importlib, json, pkgutil, sys\n"
        f"import {package} as pkg\n"
        f"mods = ['{package}.' + m.name for m in pkgutil.iter_modules(pkg.__path__)]\n"
        "for m in mods: importlib.import_module(m)\n"
        f"forbidden = {forbidden!r}\n"
        "leaked = sorted(m for m in sys.modules "
        "if any(m == f or m.startswith(f + '.') for f in forbidden))\n"
        "print(json.dumps({'mods': mods, 'leaked': leaked}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env={"PYTHONPATH": SRC_DIR, "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("package,allowed", [
    ("hive.core", ("hive.core",)),
    ("hive.memory", ("hive.core", "hive.memory")),
])
def test_layer_imports_only_downward(package, allowed):
    data = _probe(package, allowed)
    assert data["mods"], f"no {package}.* modules discovered"
    assert not data["leaked"], f"{package} must not import: {data['leaked']}"
