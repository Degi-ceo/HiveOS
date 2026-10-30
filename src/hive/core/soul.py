"""
soul.py — loads the immutable identity contract, verbatim and in place.

Config/SOUL.md is PROTECTED: never moved, never edited (see SOUL.md itself and
docs/references/SYNTHESIS.md Part A.4). This module only READS it, from its
canonical repo location, so the new lowercase package can use it without relocating
the file.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_repo_root(module_file: str | Path, configured_root: str | None) -> Path:
    """Locate the protected files in a source tree or an explicit wheel release.

    A wheel's module path is under site-packages, where parents[3] is not the
    repository. Never silently anchor the approval gate or file protection to
    that unrelated directory.
    """
    module_path = Path(module_file).resolve()
    source_root = module_path.parents[3]
    protected = (Path("Config/SOUL.md"), Path("Core/approval_gate.py"))
    source_layout = module_path == source_root / "src" / "hive" / "core" / "soul.py"
    configured: Path | None = None
    if configured_root:
        root = Path(configured_root).expanduser()
        if not root.is_absolute():
            raise RuntimeError("HIVE_REPO_ROOT must be an absolute path")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("HIVE_REPO_ROOT must name an existing directory") from exc
        if root != resolved or not resolved.is_dir():
            raise RuntimeError("HIVE_REPO_ROOT must name a real, non-symlink directory")
        for name in protected:
            path = resolved / name
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(resolved):
                raise RuntimeError(f"HIVE_REPO_ROOT is missing protected {name.as_posix()}")
        configured = resolved
    if source_layout:
        for name in protected:
            if not (source_root / name).is_file():
                raise RuntimeError(f"Source checkout is missing protected {name.as_posix()}")
        if configured is not None and configured != source_root:
            raise RuntimeError("HIVE_REPO_ROOT conflicts with executing source tree")
        return source_root
    if configured is None:
        raise RuntimeError("Installed HiveOS requires HIVE_REPO_ROOT with protected source files")
    return configured


REPO_ROOT = resolve_repo_root(__file__, os.getenv("HIVE_REPO_ROOT"))
SOUL_PATH = REPO_ROOT / "Config" / "SOUL.md"


def load_soul() -> str:
    """Return the SOUL.md contents verbatim. Raises if missing (must exist)."""
    return SOUL_PATH.read_text(encoding="utf-8")


_SOUL: str | None = None


def __getattr__(name: str) -> str:
    """Lazy `soul.SOUL` (PEP 562): importing this module for REPO_ROOT/SOUL_PATH does
    NOT read the file; SOUL is read + cached on first access. Keeps `import
    hive.core.config` side-effect-free and tolerant of a transiently-absent SOUL.md."""
    if name == "SOUL":
        global _SOUL
        if _SOUL is None:
            _SOUL = load_soul()
        return _SOUL
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
