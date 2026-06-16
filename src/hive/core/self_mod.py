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
# cmd may be a list (exec, safe) or a plain string (shell, for trusted git sub-commands).
Runner = Callable[[str | list[str], str | None], Awaitable[tuple[int, str]]]
# (worktree_path) -> list of changed repo-relative paths
ApplyFn = Callable[[str], Awaitable[list[str]]]


async def _default_run(cmd: str | list[str], cwd: str | None = None) -> tuple[int, str]:
    if isinstance(cmd, list):
        # Use exec (no shell interpretation) for commands with LLM-sourced arguments.
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    else:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode()


# (branch, title, body) -> PR url (or None if opening failed). Injected so self_mod
# stays testable without the network; the runtime wires github_pr_opener().
PROpener = Callable[[str, str, str], Awaitable[str | None]]


def github_pr_opener(token: str, owner: str, repo: str, *, base: str = "main",
                     draft: bool = True) -> PROpener:
    """Real PR opener over the GitHub REST API using Hive's own token (#si-3).

    Hive opens a DRAFT PR from its pushed branch and NEVER merges — a human merges
    (SOUL.md hard rule). httpx is imported lazily so importing self_mod never
    requires it."""
    async def open_pr(branch: str, title: str, body: str) -> str | None:
        if not (token and owner and repo):
            return None
        import httpx
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        payload = {"title": title, "head": branch, "base": base,
                   "body": body, "draft": draft}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, json=payload, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                })
            if r.status_code in (200, 201):
                return r.json().get("html_url")
            log.warning("PR open failed (%s): %s", r.status_code, r.text[:300])
        except Exception as exc:  # noqa: BLE001 - PR opening is best-effort
            log.warning("PR open error: %s", exc)
        return None

    return open_pr


_PROTECTED_NAMES = {p.rsplit("/", 1)[-1].lower() for p in PROTECTED_PATHS}
_PROTECTED_PATHS_LOWER = {p.lower().replace("\\", "/") for p in PROTECTED_PATHS}


def _touches_protected(changed: list[str]) -> bool:
    """True if any changed path is a PROTECTED file."""
    for cp in changed:
        norm = cp.replace("\\", "/").lower()
        if any(norm == pp or norm.endswith("/" + pp) for pp in _PROTECTED_PATHS_LOWER):
            return True
        basename = norm.rsplit("/", 1)[-1]
        if basename in _PROTECTED_NAMES:
            return True
    return False


class SelfModifier:
    def __init__(self, *, repo_root: str = ".", run: Runner | None = None,
                 test_cmd: str = "python -m pytest -q",
                 open_pr: PROpener | None = None) -> None:
        self._root = repo_root
        self._run = run or _default_run
        self._test_cmd = test_cmd
        self._open_pr = open_pr

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
                log.warning("self_mod BLOCKED: proposed edit touches protected files: %s",
                            [p for p in changed if _touches_protected([p])])
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
            # Use list form (exec, not shell) so LLM-sourced title cannot inject shell.
            await self._run(["git", "commit", "-m", title], wt)
            rc, push_out = await self._run(f"git push -u origin {branch}", wt)
            if rc != 0:
                # Push failed (auth/network) — surface it instead of falsely reporting ok.
                return {"ok": False, "stage": "push", "branch": branch,
                        "last_good": last_good, "log": push_out[-500:]}

            result = {"ok": True, "stage": "pushed", "branch": branch,
                      "last_good": last_good, "push": push_out[-500:]}
            # #si-3: open a DRAFT PR via the GitHub REST API; never merge (human merges).
            if self._open_pr is not None:
                pr_url = await self._open_pr(branch, title, description)
                result["pr_url"] = pr_url
                result["note"] = ("draft PR opened by Hive; a human merges"
                                  if pr_url else "branch pushed; PR open failed (see logs)")
            else:
                result["note"] = "branch pushed; open a PR to review (Hive never merges)"
            return result
        finally:
            rc, out = await self._run(f"git worktree remove --force {wt}", self._root)
            if rc != 0:
                log.warning("self_mod: worktree cleanup failed for %s: %s", wt, out[:200])
            if not dry_run:
                # branch is pushed (or never created on failure); local branch is disposable
                rc, out = await self._run(f"git branch -D {branch}", self._root)
                if rc != 0:
                    log.warning("self_mod: branch cleanup failed for %s: %s", branch, out[:200])
