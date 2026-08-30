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

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

import hive

HIVE_DIR = pathlib.Path(hive.__file__).resolve().parent
SRC_DIR = str(HIVE_DIR.parent)

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
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC_DIR},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("package,allowed", [
    ("hive.core", ("hive.core",)),
    ("hive.memory", ("hive.core", "hive.memory")),
    ("hive.context", ("hive.core", "hive.context")),
    ("hive.tools", ("hive.core", "hive.tools")),
    ("hive.observability", ("hive.core", "hive.observability")),
    ("hive.agents", ("hive.core", "hive.llm", "hive.tools", "hive.memory",
                     "hive.context", "hive.agents")),
])
def test_layer_imports_only_downward(package, allowed):
    data = _probe(package, allowed)
    assert data["mods"], f"no {package}.* modules discovered"
    assert not data["leaked"], f"{package} must not import: {data['leaked']}"


def _static_imports(py_file: pathlib.Path) -> set[str]:
    """Every `hive.*` module referenced by import statements ANYWHERE in the file —
    including function-local imports the runtime probe above cannot see."""
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("hive."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.startswith("hive."):
                found.add(node.module)
    return found


def test_core_has_no_higher_layer_imports_even_local():
    """Static AST scan: `hive.core.*` must not import any higher layer, even via a
    function-local import (which the sys.modules probe cannot catch — this is the
    exact gap that let a core->llm dependency slip in once)."""
    higher = tuple(L for L in _ALL_LAYERS if L != "hive.core")
    offenders: dict[str, list[str]] = {}
    for py_file in sorted((HIVE_DIR / "core").glob("*.py")):
        bad = sorted(m for m in _static_imports(py_file)
                     if any(m == h or m.startswith(h + ".") for h in higher))
        if bad:
            offenders[py_file.name] = bad
    assert not offenders, f"core modules import higher layers: {offenders}"
