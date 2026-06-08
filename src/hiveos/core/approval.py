"""
approval.py — bridge to the PROTECTED approval gate, in place.

Core/approval_gate.py is the canonical danger firewall and is NEVER moved or
edited (see SOUL.md + Docs/references/SYNTHESIS.md Part A.4). The new package must
not duplicate it; instead it loads the existing file by path and re-exports its
symbols. The canonical logic stays the untouched Core/approval_gate.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# src/hiveos/core/approval.py -> parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
_GATE_PATH = REPO_ROOT / "Core" / "approval_gate.py"

_spec = importlib.util.spec_from_file_location("hiveos._protected_approval_gate", _GATE_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - file must exist
    raise ImportError(f"cannot load protected approval gate at {_GATE_PATH}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Re-export the canonical symbols (defined in the untouched protected file).
gate = _mod.gate
ApprovalGate = _mod.ApprovalGate
PROTECTED_PATHS = _mod.PROTECTED_PATHS
DANGEROUS_TOOLS = _mod.DANGEROUS_TOOLS

__all__ = ["gate", "ApprovalGate", "PROTECTED_PATHS", "DANGEROUS_TOOLS"]
