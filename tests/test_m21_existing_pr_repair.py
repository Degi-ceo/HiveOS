"""Existing-PR repair must reproduce exact remote head and never create a new PR."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier, repair_evidence_withheld


BRANCH = "hive/auto-" + "a" * 32
HEAD = "1" * 40
NEW_HEAD = "2" * 40
TREE = "3" * 40
EVAL = "4" * 40
PATH = "docs/repair-note.md"


class FakeGit:
    def __init__(self, *, initial_rc=1, remote_head=HEAD, changed=PATH,
                 test_rcs=None, checkout_rc=0, push_rc=0,
                 test_output="FAIL exact head\n", prior_diff="+prior failing change\n"):
        self.calls = []
        self.tests = []
        self.initial_rc = initial_rc
        self.remote_head = remote_head
        self.changed = changed
        self.pushes = 0
        self.committed = False
        self.test_rcs = test_rcs
        self.checkout_rc = checkout_rc
        self.push_rc = push_rc
        self.test_output = test_output
        self.prior_diff = prior_diff
        self.tree = TREE

    async def __call__(self, cmd, cwd=None):
        args = cmd if isinstance(cmd, list) else cmd.split()
        self.calls.append((args, cwd))
        if args[:2] == ["git", "ls-remote"]:
            return 0, f"{self.remote_head}\trefs/heads/{BRANCH}\n"
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return 0, HEAD + "\n"
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, (NEW_HEAD if self.committed else HEAD) + "\n"
        if args[:3] == ["git", "rev-parse", "HEAD^"]:
            return 0, HEAD + "\n"
        if args[:3] == ["git", "rev-parse", "HEAD^{tree}"]:
            return 0, self.tree + "\n"
        if args[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, BRANCH + "\n"
        if args[:2] == ["git", "write-tree"]:
            return 0, self.tree + "\n"
        if "commit-tree" in args:
            return 0, EVAL + "\n"
        if args[:3] == ["git", "diff", "--name-only"]:
            return 0, self.changed + "\n"
        if args[:3] == ["git", "diff", "--cached"] and "--name-only" in args:
            return 0, self.changed + "\n"
        if args[:3] == ["git", "diff", "--cached"]:
            return 0, "+safe change\n"
        if args[:2] == ["git", "diff"] and HEAD + "^" in args:
            return 0, self.prior_diff
        if args[:2] == ["git", "ls-files"]:
            return 0, ""
        if args[:2] == ["git", "status"]:
            return 0, "M " + self.changed + "\n"
        if args[:2] == ["git", "commit"]:
            self.committed = True
            return 0, "ok"
        if args[:2] == ["git", "push"]:
            self.pushes += 1
            if self.push_rc == 0:
                self.remote_head = NEW_HEAD
            return self.push_rc, "ok"
        if args[:3] == ["git", "worktree", "add"] and "--detach" in args:
            return self.checkout_rc, "checkout error" if self.checkout_rc else "ok"
        if args and args[0] == "pytest":
            self.tests.append(cwd)
            rc = (self.test_rcs[len(self.tests) - 1]
                  if self.test_rcs is not None else self.initial_rc if len(self.tests) == 1 else 0)
            return rc, self.test_output if rc else "passed"
        return 0, "ok"


async def _gate(_wt, _base, _paths, _run_id, digest):
    return {"ok": True, "candidate_digest": digest}


def test_exact_head_reproduction_then_same_branch_nonforce_push():
    git = FakeGit()
    observed = []
    repaired = []
    opened = []

    async def verify(branch, head_sha):
        observed.append((branch, head_sha))
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(failure):
        repaired.append(failure)

        async def apply(_wt):
            return [PATH]

        return apply

    async def opener(*args):
        opened.append(args)

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest", open_pr=opener)
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", run_id="run-1",
        candidate_gate=_gate,
    ))
    assert out["ok"] and out["stage"] == "pushed", (out, git.calls)
    assert out["head_sha"] == NEW_HEAD
    assert len(repaired) == 1 and repaired[0].attempt == 0
    assert repaired[0].run_id == "run-1"
    assert repaired[0].changed_paths == (PATH,)
    assert "FAIL exact head" in repaired[0].test_log
    assert "prior failing change" in repaired[0].staged_diff
    assert observed == [(BRANCH, HEAD), (BRANCH, HEAD)]
    assert len([c for c, _ in git.calls if c[:3] == ["git", "worktree", "add"] and "-b" in c]) == 1
    assert len(git.tests) == 2
    pushes = [c for c, _ in git.calls if c[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "--porcelain", "origin", f"HEAD:refs/heads/{BRANCH}"]]
    assert not opened


def test_authenticated_review_edit_on_green_head_reuses_candidate_gates():
    git = FakeGit(initial_rc=0)
    verified = []

    async def verify(branch, sha):
        verified.append((branch, sha))
        return {"ok": True, "branch": branch, "head_sha": sha}

    async def apply(_wt):
        return [PATH]

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
    out = asyncio.run(mod.apply_existing_pr_review(
        BRANCH, HEAD, verify, apply, run_id="review-run", candidate_gate=_gate,
    ))
    assert out["ok"] and out["stage"] == "pushed", (out, git.calls)
    assert verified == [(BRANCH, HEAD), (BRANCH, HEAD)]
    assert len(git.tests) == 1  # no failing-head reproduction precondition
    assert git.pushes == 1
    assert not any("--force" in cmd for cmd, _ in git.calls if cmd[:2] == ["git", "push"])


def test_review_identity_race_and_source_edit_never_push():
    for path, stale in ((PATH, True), ("src/hive/runtime.py", False)):
        git = FakeGit(initial_rc=0, changed=path)
        checks = []

        async def verify(branch, sha):
            checks.append((branch, sha))
            return {"ok": not stale or len(checks) == 1,
                    "branch": branch, "head_sha": sha}

        async def apply(_wt):
            return [path]

        mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
        out = asyncio.run(mod.apply_existing_pr_review(
            BRANCH, HEAD, verify, apply, run_id="review-run", candidate_gate=_gate,
        ))
        assert out["stage"] == ("pr_identity" if stale else "review_required")
        assert git.pushes == 0


def test_untrusted_source_edit_requires_review_even_when_generic_auto_policy_allows_it():
    for path in ("tests/test_new_fix.py", "gateway/router.py", "src/frontend/app.js"):
        git = FakeGit(changed=path)

        async def verify(branch, head_sha):
            return {"ok": True, "branch": branch, "head_sha": head_sha}

        async def repair(_failure):
            async def apply(_wt):
                return [path]

            return apply

        mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
        out = asyncio.run(mod.repair_existing_pr(
            BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
        ))
        assert out["stage"] == "review_required", (path, out)
        assert git.pushes == 0


def test_identity_or_remote_head_mismatch_stops_before_repair():
    for bad_identity, remote in [(True, HEAD), (False, "9" * 40)]:
        git = FakeGit(remote_head=remote)
        repairs = []

        async def verify(branch, head_sha):
            if bad_identity:
                return {"ok": False, "branch": branch, "head_sha": head_sha}
            return {"ok": True, "branch": branch, "head_sha": head_sha}

        async def repair(failure):
            repairs.append(failure)
            return None

        mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
        out = asyncio.run(mod.repair_existing_pr(
            BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
        ))
        assert not out["ok"]
        assert not repairs and not git.tests and not git.pushes


def test_initial_test_checkout_failure_or_green_head_never_calls_repair():
    for git in (FakeGit(checkout_rc=1), FakeGit(initial_rc=0)):
        repairs = []

        async def verify(branch, head_sha):
            return {"ok": True, "branch": branch, "head_sha": head_sha}

        async def repair(failure):
            repairs.append(failure)
            return None

        mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
        out = asyncio.run(mod.repair_existing_pr(
            BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
        ))
        assert not out["ok"] and not repairs and not git.pushes
        assert out["stage"] in {"test", "ci_unreproducible"}


def test_fresh_identity_race_before_push_fails_closed():
    git = FakeGit()
    checks = []

    async def verify(branch, head_sha):
        checks.append(head_sha)
        return {"ok": len(checks) == 1, "branch": branch, "head_sha": head_sha}

    async def repair(_failure):
        async def apply(_wt):
            return [PATH]
        return apply

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
    ))
    assert out["stage"] == "pr_identity"
    assert len(checks) == 2 and git.pushes == 0


def test_repair_attempts_stay_in_one_candidate_worktree_and_are_bounded():
    git = FakeGit(test_rcs=[1, 1, 1, 0])
    failures = []

    async def verify(branch, head_sha):
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(failure):
        failures.append(failure)
        git.tree = f"{len(failures) + 3:x}" * 40

        async def apply(_wt):
            return [PATH]

        return apply

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
        max_repair_attempts=99,
    ))
    assert out["ok"] and out["repair_attempts"] == 2
    assert [failure.attempt for failure in failures] == [0, 1, 2]
    assert all(failure.changed_paths == (PATH,) for failure in failures)
    assert len(git.tests) == 4
    candidate_adds = [c for c, _ in git.calls if c[:3] == ["git", "worktree", "add"] and "-b" in c]
    assert len(candidate_adds) == 1
    assert git.pushes == 1


def test_initial_failure_evidence_redacts_config_secret():
    secret = "super-private-token"
    git = FakeGit(
        test_output=f"FAIL {secret}\n", prior_diff=f"+credential={secret}\n",
    )
    failures = []

    async def verify(branch, head_sha):
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(failure):
        failures.append(failure)
        return None

    mod = SelfModifier(
        repo_root="/tmp/existing-pr", run=git, test_cmd="pytest",
        secret_values=[secret],
    )
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
    ))
    assert out["stage"] == "repair_declined"
    assert len(failures) == 1
    assert repair_evidence_withheld(failures[0])
    assert secret not in repr(failures[0]) and secret not in repr(out)


def test_existing_pr_cannot_accept_caller_supplied_review_override():
    git = FakeGit(changed="tests/test_new_fix.py")

    async def verify(branch, head_sha):
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(_failure):
        async def apply(_wt):
            return ["tests/test_new_fix.py"]
        return apply

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
    with pytest.raises(TypeError, match="approved_review"):
        asyncio.run(mod.repair_existing_pr(
            BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
            approved_review=True,
        ))
    assert git.pushes == 0


def test_push_failure_is_uncertain_and_never_opens_pr():
    git = FakeGit(push_rc=1)
    opened = []

    async def verify(branch, head_sha):
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(_failure):
        async def apply(_wt):
            return [PATH]
        return apply

    async def opener(*args):
        opened.append(args)

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest", open_pr=opener)
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
    ))
    assert out["stage"] == "push_uncertain" and not opened
    assert git.pushes == 1


def test_repair_exhausts_after_two_extra_failed_candidates():
    git = FakeGit(test_rcs=[1, 1, 1, 1])
    failures = []

    async def verify(branch, head_sha):
        return {"ok": True, "branch": branch, "head_sha": head_sha}

    async def repair(failure):
        failures.append(failure)
        git.tree = f"{len(failures) + 3:x}" * 40

        async def apply(_wt):
            return [PATH]
        return apply

    mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
    out = asyncio.run(mod.repair_existing_pr(
        BRANCH, HEAD, verify, repair, title="repair", candidate_gate=_gate,
        max_repair_attempts=99,
    ))
    assert out["stage"] == "repair_exhausted" and out["repair_attempts"] == 2
    assert [failure.attempt for failure in failures] == [0, 1, 2]
    assert len(git.tests) == 4 and git.pushes == 0


def test_incomplete_identity_evidence_and_invalid_ref_fail_closed():
    async def verify(_branch, _head_sha):
        return {"ok": True}  # No exact branch/head proof.

    async def repair(_failure):
        raise AssertionError("must not repair")

    for branch, head in ((BRANCH, HEAD), ("hive/auto-bad;echo", HEAD), (BRANCH, "bad")):
        git = FakeGit()
        mod = SelfModifier(repo_root="/tmp/existing-pr", run=git, test_cmd="pytest")
        out = asyncio.run(mod.repair_existing_pr(
            branch, head, verify, repair, title="repair", candidate_gate=_gate,
        ))
        assert out["stage"] == "pr_identity" and not git.calls
