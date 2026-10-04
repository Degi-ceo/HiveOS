"""Conservative source revision evidence for a running HiveOS gateway.

The result is captured when the app is constructed, not recomputed from a
checkout that may change while an older process is still serving requests.
Only a clean source checkout can identify itself by a Git commit. Packaged
installs without a source checkout deliberately return no revision.
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from hive.core.child_env import minimal_worker_environment

_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SOURCE_PATHS = ("src/hive", "pyproject.toml", ".gitignore", "Config/SOUL.md", "Core/approval_gate.py")


def detect_source_revision(repo_root: Path | None = None) -> str | None:
    """Return HEAD only when the current source tree is clean and unambiguous."""
    if repo_root is None:
        return _running_source_revision()
    return _detect(repo_root.resolve())


@lru_cache(maxsize=1)
def _running_source_revision() -> str | None:
    return _detect(Path(__file__).resolve().parents[3])


def _detect(root: Path) -> str | None:
    if not (root / "src" / "hive").is_dir():
        return None
    env = minimal_worker_environment()

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ("git", "-C", str(root), *args),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=2, check=False, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    toplevel = git("rev-parse", "--show-toplevel")
    if not toplevel or Path(toplevel).resolve() != root:
        return None
    before = git("rev-parse", "--verify", "HEAD")
    if not before or not _SHA_RE.fullmatch(before):
        return None
    index_flags = git("ls-files", "-v", "--", *_SOURCE_PATHS)
    if index_flags is None or not index_flags or any(
        not line.startswith("H ") for line in index_flags.splitlines()
    ):
        # Git status trusts assume-unchanged and skip-worktree index bits.
        return None
    index_modes = git("ls-files", "-s", "--", *_SOURCE_PATHS)
    if index_modes is None or any(
        not line.startswith(("100644 ", "100755 "))
        for line in index_modes.splitlines()
    ):
        # A tracked symlink could point to mutable code outside this checkout.
        return None
    status = git("status", "--porcelain", "--untracked-files=all", "--ignored=matching",
                 "--", *_SOURCE_PATHS)
    if status is None or status:
        return None
    after = git("rev-parse", "--verify", "HEAD")
    return before if before == after else None
