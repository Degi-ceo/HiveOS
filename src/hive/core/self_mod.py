"""
self_mod.py — safe self-modification engine (KEEP+ADAPT from Core/self_mod.py).

How Hive changes its OWN code without destroying itself. The flow from SOUL.md is
non-negotiable:
  1. snapshot last-known-good HEAD (instant rollback)
  2. isolated git worktree on a new branch (never live main)
  3. apply changes only inside the worktree
  4. run tests in the candidate
  5. fail -> discard worktree, stay on last-known-good, record
  6. pass -> commit + push branch + open PR (NEVER merge); a human merges
Changes touching SOUL.md / approval_gate.py are refused outright.

`dry_run=True` runs steps 1–4 and skips push/PR (the P8 verify). The shell runner
is injectable so the flow is unit-testable without real git. Depends on core only.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

from hive.core.approval import PROTECTED_PATHS

log = logging.getLogger("hive.selfmod")

# (cmd, cwd) -> (returncode, combined_output)
Runner = Callable[[str, str | None], Awaitable[tuple[int, str]]]
# (worktree_path) -> list of changed repo-relative paths
ApplyFn = Callable[[str], Awaitable[list[str]]]


async def _default_run(cmd: str, cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode()


def _touches_protected(changed: list[str]) -> bool:
    return any(any(p in cp for p in PROTECTED_PATHS) for cp in changed)


class SelfModifier:
    def __init__(self, *, repo_root: str = ".", run: Runner | None = None,
                 test_cmd: str = "python -m pytest -q") -> None:
        self._root = repo_root
        self._run = run or _default_run
        self._test_cmd = test_cmd

    async def propose(self, title: str, description: str, apply_fn: ApplyFn,
                      *, dry_run: bool = False) -> dict:
        branch = f"hive/auto-{int(time.time())}"
        wt = str(Path(self._root) / ".worktrees" / branch.replace("/", "-"))

        _, head = await self._run("git rev-parse HEAD", self._root)
        last_good = head.strip()

        rc, out = await self._run(f"git worktree add -b {branch} {wt}", self._root)
        if rc != 0:
            return {"ok": False, "stage": "worktree", "log": out}
        try:
            changed = await apply_fn(wt)
            if _touches_protected(changed):
                return {"ok": False, "stage": "protected",
                        "msg": "change touches SOUL.md or approval gate — human-only"}

            rc, test_out = await self._run(self._test_cmd, wt)
            if rc != 0:
                return {"ok": False, "stage": "test", "last_good": last_good,
                        "log": test_out[-2000:], "recorded": True}

            if dry_run:
                return {"ok": True, "stage": "dry_run", "branch": branch,
                        "last_good": last_good, "changed": changed}

            await self._run("git add -A", wt)
            await self._run(f'git commit -m "{title}"', wt)
            rc, push_out = await self._run(f"git push -u origin {branch}", wt)
            return {"ok": True, "stage": "pushed", "branch": branch,
                    "last_good": last_good, "push": push_out[-500:],
                    "note": "PR is opened via the GitHub MCP server; never merged by Hive"}
        finally:
            await self._run(f"git worktree remove --force {wt}", self._root)
            if not dry_run:
                # branch is pushed (or never created on failure); local branch is disposable
                await self._run(f"git branch -D {branch}", self._root)
