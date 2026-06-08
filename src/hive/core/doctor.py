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

from hive.core import config
from hive.core.soul import SOUL_PATH

# Non-critical checks: absence is a warning, not a doctor failure.
_WARN_ONLY = ("MINIMAX_API_KEY", "data dir")


def check() -> list[tuple[str, bool, str]]:
    """Return [(name, ok, detail)] diagnostics."""
    cfg = config.get_config()
    results: list[tuple[str, bool, str]] = []

    results.append(("SOUL.md present", SOUL_PATH.exists(), str(SOUL_PATH)))

    gate_path = cfg.root / "Core" / "approval_gate.py"
    results.append(("approval_gate present", gate_path.exists(), str(gate_path)))

    for mod in ("hive.core.registry", "hive.core.events", "hive.core.types",
                "hive.core.config", "hive.core.approval"):
        try:
            importlib.import_module(mod)
            results.append((f"import {mod}", True, "ok"))
        except Exception as exc:  # noqa: BLE001
            results.append((f"import {mod}", False, str(exc)))

    results.append(("MINIMAX_API_KEY set", bool(cfg.minimax_api_key), "env"))
    results.append(("data dir present", cfg.data_dir.is_dir(), str(cfg.data_dir)))
    return results


def run(fix: bool = False) -> bool:
    """Print diagnostics; return True if all critical checks pass."""
    if fix:
        config.get_config().ensure_dirs()
    ok_all = True
    for name, ok, detail in check():
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}: {detail}")
        if not ok and not any(w in name for w in _WARN_ONLY):
            ok_all = False
    if fix:
        print("(no migrations to apply yet)")
    return ok_all


if __name__ == "__main__":
    import sys

    raise SystemExit(0 if run("--fix" in sys.argv) else 1)
