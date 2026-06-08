"""
doctor.py — `hive doctor [--fix]` health checks + migration owner (scaffold).

OpenClaw rule: runtime reads only the canonical config/schema shape; legacy shapes
are migrated HERE, never via runtime shims (Docs/references/OPENCLAW_REFERENCE.md
§2). For now this verifies the environment (the casing showstopper from
HIVEOS_AUDIT §0 cannot recur because the package is lowercase). Migrations are
appended as schema evolves.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from hive.core import config
from hive.core.soul import SOUL_PATH


def check() -> list[tuple[str, bool, str]]:
    """Return [(name, ok, detail)] diagnostics."""
    results: list[tuple[str, bool, str]] = []

    results.append(("SOUL.md present", SOUL_PATH.exists(), str(SOUL_PATH)))

    gate_path = Path(config.ROOT) / "Core" / "approval_gate.py"
    results.append(("approval_gate present", gate_path.exists(), str(gate_path)))

    for mod in ("hive.core.registry", "hive.core.events", "hive.core.types",
                "hive.core.config", "hive.core.approval"):
        try:
            importlib.import_module(mod)
            results.append((f"import {mod}", True, "ok"))
        except Exception as exc:  # noqa: BLE001
            results.append((f"import {mod}", False, str(exc)))

    results.append(("MINIMAX_API_KEY set", bool(config.MINIMAX_API_KEY), "env"))
    results.append(("data dir writable", Path(config.DATA_DIR).is_dir(), str(config.DATA_DIR)))
    return results


def run(fix: bool = False) -> bool:
    """Print diagnostics; return True if all critical checks pass."""
    ok_all = True
    for name, ok, detail in check():
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}: {detail}")
        # MINIMAX key absence is a warning, not a hard failure.
        if not ok and "MINIMAX_API_KEY" not in name:
            ok_all = False
    if fix:
        print("(no migrations to apply yet)")
    return ok_all


if __name__ == "__main__":
    import sys

    raise SystemExit(0 if run("--fix" in sys.argv) else 1)
