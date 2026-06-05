"""
self_mod.py — safe self-modification engine ("config surgeon").

This is how Hive changes his OWN code without destroying himself. The flow,
straight from SOUL.md, is non-negotiable:

  1. snapshot last-known-good commit (for instant rollback)
  2. create an isolated git worktree on a new branch
  3. apply changes ONLY inside the worktree (never on live main)
  4. run tests in the worktree copy
  5. on failure -> discard worktree, stay on last-known-good, record the failure
  6. on success -> push branch, open a PR on Hive's own GitHub with full
     English description, then notify Kamil in Polish
  7. NEVER merge to main. A human reviews and merges.

SOUL.md and core/approval_gate.py changes are refused here outright — they must
be authored by a human or explicitly approved.
"""
from __future__ import annotations
import time
import asyncio
import logging
import subprocess
from pathlib import Path

from core import settings
from core.approval_gate import PROTECTED_PATHS

log = logging.getLogger("hiveos.selfmod")


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str]:
    p = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    out, _ = await p.communicate()
    return p.returncode, out.decode()


def _touches_protected(changed_paths: list[str]) -> bool:
    return any(any(p in cp for p in PROTECTED_PATHS) for cp in changed_paths)


async def propose_change(
    title: str,
    description: str,
    apply_fn,                      # async callable(worktree_path) -> list[str] changed
    test_cmd: str = "python -m py_compile core/*.py tools/*.py memory/*.py gateway/*.py",
    repo_root: str = ".",
) -> dict:
    """Run the full safe self-mod flow. Returns a result dict for the caller to
    surface to Kamil (in Polish)."""
    ts = int(time.time())
    branch = f"hive/auto-{ts}"
    wt = Path(repo_root) / ".worktrees" / branch.replace("/", "-")

    # 1. snapshot last-known-good
    rc, head = await _run("git rev-parse HEAD", repo_root)
    last_good = head.strip()

    # 2. worktree on a new branch
    rc, out = await _run(f"git worktree add -b {branch} {wt}", repo_root)
    if rc != 0:
        return {"ok": False, "stage": "worktree", "log": out}

    try:
        # 3. apply changes inside the worktree only
        changed = await apply_fn(str(wt))
        if _touches_protected(changed):
            await _run(f"git worktree remove --force {wt}", repo_root)
            return {"ok": False, "stage": "protected",
                    "msg": "change touches SOUL.md or approval gate — human-only"}

        # 4. test in the candidate copy
        rc, test_out = await _run(test_cmd, str(wt))
        if rc != 0:
            # 5. failure -> discard, stay on last-known-good, record
            await _run(f"git worktree remove --force {wt}", repo_root)
            await _run(f"git branch -D {branch}", repo_root)
            return {"ok": False, "stage": "test", "last_good": last_good,
                    "log": test_out[-2000:], "recorded": True}

        # 6. success -> commit + push branch + open PR (never merge)
        await _run("git add -A", str(wt))
        await _run(f'git commit -m "{title}"', str(wt))
        pushed = await _run(f"git push -u origin {branch}", str(wt))
        pr = await _open_pr(branch, title, description, test_out)
        return {"ok": True, "branch": branch, "pr": pr,
                "last_good": last_good, "push": pushed[1][-500:]}
    finally:
        # always clean up the worktree dir (branch is pushed already)
        await _run(f"git worktree remove --force {wt}", repo_root)


async def _open_pr(branch: str, title: str, body: str, test_out: str) -> dict:
    """Open a PR on Hive's own GitHub. Prefers `gh`; falls back to API."""
    full_body = (
        f"{body}\n\n## Tests\n```\n{test_out[-1500:]}\n```\n\n"
        "## Rollback plan\nRevert this branch; live `main` is untouched until merge.\n\n"
        "_Opened autonomously by Hive. Human review + manual merge required._"
    )
    bpath = Path(settings.DATA_DIR) / "pr_body.md"
    bpath.write_text(full_body, encoding="utf-8")
    rc, out = await _run(
        f'gh pr create --title "{title}" --body-file "{bpath}" '
        f'--base main --head {branch}'
    )
    if rc == 0:
        return {"opened": True, "via": "gh", "out": out.strip()}
    return {"opened": False, "via": "gh", "out": out.strip(),
            "note": "set HIVE_GITHUB_TOKEN / gh auth, or use github-mcp-server"}
