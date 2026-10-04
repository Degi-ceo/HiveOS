"""Opt-in runtime CI feedback uses only authenticated, doc-only repairs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hive.core.config import HiveConfig
from hive.core.self_mod import CandidateFailure
from hive.core.spec_search import Edit, EditOp
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS, _feedback_doc_target


URL = "https://github.com/owner/repo/pull/42"
BRANCH = "hive/auto-" + "a" * 32
OLD = "1" * 40
NEW = "2" * 40


def _snapshot(sha=OLD, **overrides):
    snapshot = {
        "number": 42, "url": URL, "state": "open", "status": "draft",
        "draft": True, "ci_state": "failed", "ownership_verified": True,
        "head_sha": sha, "pr_id": 99, "author_id": 4,
        "head_repo_id": 8, "base_repo_id": 8,
        "head_ref": BRANCH, "base_ref": "main",
    }
    snapshot.update(overrides)
    return snapshot


class _Ledger:
    def __init__(self):
        self.rounds = []
        self.head = OLD
        self.reservations = 0
        self.standdown = None

    def find_selfmod_run_id(self, *, pr_url):
        assert pr_url == URL
        return "run-42"

    def validate_pr_identity(self, run_id, pr_url, snapshot):
        return (
            (run_id, pr_url) == ("run-42", URL)
            and snapshot.get("url") == URL
            and snapshot.get("pr_id") == 99
            and snapshot.get("author_id") == 4
            and snapshot.get("head_repo_id") == 8
            and snapshot.get("base_repo_id") == 8
            and snapshot.get("head_ref") == BRANCH
            and snapshot.get("base_ref") == "main"
            and snapshot.get("head_sha") == self.head
        )

    def get_pr_identity(self, pr_url):
        assert pr_url == URL
        return {
            "bound": True, "pr_number": 42, "pr_id": 99,
            "author_id": 4, "head_repo_id": 8, "base_repo_id": 8,
            "branch": BRANCH, "base_ref": "main", "pushed_sha": self.head,
        }

    def get_pr_feedback_rounds(self, pr_url):
        assert pr_url == URL
        return [dict(row) for row in self.rounds]

    def get_pr_standdown(self, pr_url):
        assert pr_url == URL
        return {"marker": self.standdown} if self.standdown is not None else None

    def reserve_pr_feedback_round(self, run_id, pr_url, snapshot, *, feedback_key):
        assert (run_id, pr_url, feedback_key) == ("run-42", URL, f"ci:{OLD}")
        assert self.validate_pr_identity(run_id, pr_url, snapshot)
        self.reservations += 1
        self.rounds.append({"round": 1, "state": "reserved"})
        return {"round": 1, "expected_sha": OLD}

    def finish_pr_feedback_round(self, pr_url, round_number, *, state, new_sha=""):
        assert (pr_url, round_number) == (URL, 1)
        self.rounds[0]["state"] = state
        if state == "pushed":
            self.head = new_sha
        return True

    def reserve_pr_standdown(self, run_id, pr_url, snapshot, *, reason_code):
        assert (run_id, pr_url, reason_code) == ("run-42", URL, "repair_failed")
        assert self.validate_pr_identity(run_id, pr_url, snapshot)
        if self.standdown is not None:
            return None
        self.standdown = "c4ef9136-5e56-4dc1-8b77-3537de141de0"
        return self.standdown

    def mark_pr_standdown(self, pr_url, marker, *, state):
        assert (pr_url, marker) == (URL, self.standdown)
        assert state in {"posted", "uncertain"}
        return True


class _Observer:
    available = True

    def __init__(self, *, move_head_on_third=True):
        self.calls = 0
        self.move_head_on_third = move_head_on_third

    async def observe(self, number):
        assert number == 42
        self.calls += 1
        moved = self.move_head_on_third and self.calls >= 3
        return SimpleNamespace(as_dict=lambda: _snapshot(
            NEW if moved else OLD,
            status="draft", ci_state="pending" if moved else "failed",
        ))


class _Modifier:
    def __init__(self, *, path="docs/guide.md", test_log="FAILED docs check",
                 staged_diff="+old docs sentence"):
        self.path = path
        self.test_log = test_log
        self.staged_diff = staged_diff
        self.calls = 0
        self.repair_supplied = False

    async def repair_existing_pr(self, branch, sha, verify, repair, **kwargs):
        self.calls += 1
        assert (branch, sha) == (BRANCH, OLD)
        assert kwargs["run_id"] == "run-42"
        assert callable(kwargs["candidate_gate"])
        assert await verify(branch, sha) == {
            "ok": True, "branch": BRANCH, "head_sha": OLD,
        }
        apply = await repair(CandidateFailure(
            attempt=0, test_log=self.test_log, fingerprint="fp",
            staged_diff=self.staged_diff, run_id="run-42",
            changed_paths=(self.path,),
        ))
        self.repair_supplied = apply is not None
        return ({"ok": True, "stage": "pushed", "head_sha": NEW}
                if apply is not None else {"ok": False, "stage": "repair_declined"})


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    hive = HiveOS.build(config)
    hive.config = replace(
        hive.config, pr_feedback_enabled=True, github_owner="owner",
        github_repo="repo", github_token="test-token",
    )
    ledger = _Ledger()
    for name in (
        "find_selfmod_run_id", "validate_pr_identity", "get_pr_identity",
        "get_pr_feedback_rounds", "get_pr_standdown", "reserve_pr_feedback_round",
        "finish_pr_feedback_round", "reserve_pr_standdown", "mark_pr_standdown",
    ):
        monkeypatch.setattr(hive.observability_ledger, name, getattr(ledger, name))
    monkeypatch.setattr(hive.budgeter, "is_near_cap", lambda: False)
    hive.pr_observer = _Observer()
    modifier = _Modifier()
    hive.self_modifier = modifier
    edits = []

    def factory(edit):
        edits.append(edit)

        async def generate(_failure):
            async def apply(_worktree):
                return [edit.target_files[0]]

            return apply

        return generate

    hive.feedback_repair_factory = factory
    try:
        yield hive, ledger, modifier, edits
    finally:
        asyncio.run(hive.aclose())


@pytest.mark.parametrize("path", (
    "src/hive/runtime.py", "docs/../src/hive/runtime.py", "docs\\guide.md",
    "docs/.hidden.md", "docs/guide.py", "docs/one.md\ndocs/two.md",
))
def test_document_target_rejects_non_text_or_ambiguous_path(path):
    failure = CandidateFailure(0, "failed", "fp", changed_paths=(path,))
    assert _feedback_doc_target(failure) == ""


def test_document_target_requires_one_plain_file():
    failure = CandidateFailure(0, "failed", "fp", changed_paths=("docs/guide.md",))
    assert _feedback_doc_target(failure) == "docs/guide.md"
    assert _feedback_doc_target(replace(
        failure, changed_paths=("docs/guide.md", "docs/other.md"),
    )) == ""


def test_draft_failed_ci_reaches_doc_repair_and_confirms_new_head(runtime):
    hive, ledger, modifier, edits = runtime
    result = asyncio.run(hive.react_to_failed_pr_ci(_snapshot()))
    assert result == {"status": "pushed", "round": 1, "head_sha": NEW}
    assert (ledger.reservations, hive.pr_observer.calls, modifier.calls) == (1, 3, 1)
    assert len(edits) == 1
    assert edits[0].op is EditOp.EDIT_DOCS
    assert edits[0].target_files == ["docs/guide.md"]
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "wait"}
    assert ledger.reservations == 1


def test_non_doc_target_spends_one_failed_round_without_model_edit(runtime):
    hive, ledger, modifier, edits = runtime
    modifier.path = "src/hive/runtime.py"
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "failed"}
    assert modifier.calls == 1 and not modifier.repair_supplied
    assert edits == [] and ledger.rounds == [{"round": 1, "state": "failed"}]
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {
        "status": "stand_down_required",
    }
    assert ledger.reservations == 1


def test_failed_repair_posts_one_fixed_standdown_comment(runtime):
    hive, ledger, modifier, edits = runtime
    hive.pr_observer = _Observer(move_head_on_third=False)
    modifier.path = "src/hive/runtime.py"
    posted = []

    class Commenter:
        async def post_standdown(self, number, *, marker, reason_code, failed_checks):
            posted.append((number, marker, reason_code, failed_checks))
            return 751

    hive.pr_commenter = Commenter()
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "failed"}
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {
        "status": "posted", "comment_id": 751,
    }
    assert len(posted) == 1 and posted[0][0] == 42
    assert posted[0][2] == "repair_failed"
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "wait"}
    assert len(posted) == 1 and edits == [] and ledger.reservations == 1


def test_secret_bearing_test_log_never_reaches_repair_model(runtime):
    hive, ledger, modifier, edits = runtime
    hive.config = replace(hive.config, secret="private-repair-token")
    modifier.test_log = "FAILED: private-repair-token"
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "failed"}
    assert edits == [] and ledger.reservations == 1


@pytest.mark.parametrize("field,placeholder", (
    ("test_log", "[redacted credential-bearing candidate evidence]"),
    ("staged_diff", "[omitted oversized candidate evidence]"),
))
def test_withheld_candidate_evidence_never_reaches_model(runtime, field, placeholder):
    hive, ledger, modifier, edits = runtime
    setattr(modifier, field, placeholder)
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "failed"}
    assert edits == [] and ledger.reservations == 1


def test_disabled_unowned_and_budget_cap_never_reserve(runtime, monkeypatch):
    hive, ledger, modifier, _ = runtime
    hive.config = replace(hive.config, pr_feedback_enabled=False)
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {"status": "disabled"}
    hive.config = replace(hive.config, pr_feedback_enabled=True)
    assert asyncio.run(hive.react_to_failed_pr_ci(
        _snapshot(ownership_verified=False),
    )) == {"status": "wait"}
    monkeypatch.setattr(hive.budgeter, "is_near_cap", lambda: True)
    assert asyncio.run(hive.react_to_failed_pr_ci(_snapshot())) == {
        "status": "budget_deferred",
    }
    assert (ledger.reservations, modifier.calls, hive.pr_observer.calls) == (0, 0, 0)


def test_real_repair_factory_applies_only_exact_doc_replacement(tmp_path):
    class Router:
        def __init__(self):
            self.prompts = []

        async def complete(self, messages, **_kwargs):
            self.prompts.append(messages[0].content)
            return CompletionResult(
                text=json.dumps({
                    "old_text": "Previous guidance.",
                    "new_text": "Corrected guidance.",
                }),
                model="test-model",
            )

        async def aclose(self):
            pass

    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
        pr_feedback_enabled=False,
    )
    router = Router()
    hive = HiveOS.build(config, router=router)
    try:
        async def unused_apply(_worktree):
            return []

        repair = hive.feedback_repair_factory(Edit(
            op=EditOp.EDIT_DOCS, summary="Repair failing PR documentation",
            apply=unused_apply, target_files=["docs/guide.md"],
        ))
        assert repair is not None
        failure = CandidateFailure(
            attempt=0, test_log="FAILED docs check", fingerprint="fp",
            staged_diff="+Previous guidance.", changed_paths=("docs/guide.md",),
        )
        apply = asyncio.run(repair(failure))
        assert apply is not None
        assert len(router.prompts) == 1
        assert "<untrusted-content" in router.prompts[0]
        candidate = tmp_path / "candidate"
        target = candidate / "docs" / "guide.md"
        target.parent.mkdir(parents=True)
        target.write_text("Previous guidance.\n", encoding="utf-8")
        assert asyncio.run(apply(str(candidate))) == ["docs/guide.md"]
        assert target.read_text(encoding="utf-8") == "Corrected guidance.\n"
    finally:
        asyncio.run(hive.aclose())
