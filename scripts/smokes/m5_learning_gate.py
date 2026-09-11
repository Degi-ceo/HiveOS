"""Real-git smoke for the M5 in-worktree self-modification quality gate."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from hive.core.child_env import without_privileged_credentials
from hive.core.self_mod import SelfModifier


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        env=without_privileged_credentials(),
    )


async def _run() -> dict:
    with tempfile.TemporaryDirectory(prefix="hive-m5-selfmod-") as raw_root:
        root = Path(raw_root)
        _git(root, "init")
        _git(root, "config", "user.name", "HiveOS M5 Smoke")
        _git(root, "config", "user.email", "hive-smoke@example.invalid")
        (root / "candidate.txt").write_text("baseline\n", encoding="utf-8")
        _git(root, "add", "candidate.txt")
        _git(root, "commit", "-m", "baseline")
        modifier = SelfModifier(repo_root=str(root), test_cmd="git diff --check")

        async def apply(worktree: str) -> list[str]:
            (Path(worktree) / "candidate.txt").write_text(
                "baseline\ncandidate\n", encoding="utf-8",
            )
            return ["candidate.txt"]

        async def accept_gate(worktree, base_commit, changed, run_id, candidate_digest):
            assert (Path(worktree) / "candidate.txt").read_text(encoding="utf-8").endswith(
                "candidate\n"
            )
            return {
                "ok": True, "reason": "candidate measured in live worktree",
                "base_commit": base_commit, "changed": changed, "run_id": run_id,
                "candidate_digest": candidate_digest,
            }

        accepted = await modifier.propose(
            "M5 accepted smoke", "real worktree", apply,
            dry_run=True, run_id="m5-smoke-accept", candidate_gate=accept_gate,
        )

        async def reject_gate(*_args):
            return {"ok": False, "reason": "deliberate regression probe"}

        rejected = await modifier.propose(
            "M5 rejected smoke", "real worktree", apply,
            dry_run=True, run_id="m5-smoke-reject", candidate_gate=reject_gate,
        )
        assert accepted["ok"] and accepted["stage"] == "dry_run"
        assert not rejected["ok"] and rejected["stage"] == "evaluation"
        return {
            "accepted": {
                "ok": accepted["ok"], "stage": accepted["stage"],
                "reason": accepted["evaluation"]["reason"],
            },
            "rejected": {
                "ok": rejected["ok"], "stage": rejected["stage"],
                "reason": rejected["evaluation"]["reason"],
            },
        }


def main() -> int:
    print(json.dumps(asyncio.run(_run()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
