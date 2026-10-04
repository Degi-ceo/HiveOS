"""A PR feedback round must be reserved and remotely confirmed before advancing."""

import asyncio

from hive.core.pr_feedback import repair_failed_ci_once


URL = "https://github.com/owner/repo/pull/42"
OLD = "a" * 40
NEW = "b" * 40


def _snapshot(sha=OLD, **overrides):
    value = {
        "number": 42, "url": URL, "state": "open", "status": "checks_failed",
        "ci_state": "failed", "ownership_verified": True, "head_sha": sha,
        "pr_id": 99, "author_id": 4, "head_repo_id": 8, "base_repo_id": 8,
        "head_ref": "hive/auto-test", "base_ref": "main",
    }
    value.update(overrides)
    return value


class Ledger:
    def __init__(self, *, authenticated=True, rounds=()):
        self.authenticated = authenticated
        self.rounds = list(rounds)
        self.reservation = None
        self.finished = []

    def validate_pr_identity(self, run_id, pr_url, snapshot):
        assert (run_id, pr_url) == ("run-42", URL)
        return self.authenticated and snapshot["head_sha"] == OLD

    def get_pr_identity(self, pr_url):
        assert pr_url == URL
        return {
            "pr_url": URL, "run_id": "run-42", "pr_number": 42,
            "pushed_sha": OLD, "branch": "hive/auto-test", "pr_id": 99,
            "author_id": 4, "head_repo_id": 8, "base_repo_id": 8,
            "base_ref": "main", "bound": True,
        }

    def get_pr_feedback_rounds(self, pr_url):
        assert pr_url == URL
        return [dict(row) for row in self.rounds]

    def reserve_pr_feedback_round(self, run_id, pr_url, snapshot, *, feedback_key):
        assert (run_id, pr_url, snapshot["head_sha"]) == ("run-42", URL, OLD)
        assert feedback_key == f"ci:{OLD}"
        if self.reservation is not None:
            return None
        self.reservation = {"round": 1, "expected_sha": OLD}
        self.rounds.append({"round": 1, "state": "reserved"})
        return self.reservation

    def finish_pr_feedback_round(self, pr_url, round_number, *, state, new_sha=""):
        self.finished.append((pr_url, round_number, state, new_sha))
        self.rounds[round_number - 1]["state"] = state
        return True


class Observer:
    def __init__(self, snapshot=None, error=None, before=None):
        self.before = before or _snapshot()
        self.snapshot = snapshot or _snapshot(NEW, status="checks_pending", ci_state="pending")
        self.error = error
        self.calls = []

    async def observe(self, number):
        self.calls.append(number)
        if len(self.calls) == 1:
            return self.before
        if self.error:
            raise self.error
        return self.snapshot


class Repairer:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "stage": "pushed", "head_sha": NEW}
        self.calls = []

    async def __call__(self, branch, expected_sha):
        self.calls.append((branch, expected_sha))
        return self.result


def _run(ledger=None, observer=None, repairer=None, snapshot=None):
    ledger = ledger or Ledger()
    observer = observer or Observer()
    repairer = repairer or Repairer()
    result = asyncio.run(repair_failed_ci_once(
        ledger, observer, repairer, run_id="run-42", pr_url=URL,
        snapshot=snapshot or _snapshot(),
    ))
    return result, ledger, observer, repairer


def test_confirmed_new_head_advances_exactly_one_round():
    result, ledger, observer, repairer = _run()
    assert result == {"status": "pushed", "round": 1, "head_sha": NEW}
    assert repairer.calls == [("hive/auto-test", OLD)]
    assert observer.calls == [42, 42]
    assert ledger.finished == [(URL, 1, "pushed", NEW)]


def test_unowned_or_ineligible_pr_does_not_reserve_or_push():
    for ledger, snapshot in (
        (Ledger(authenticated=False), _snapshot()),
        (Ledger(), _snapshot(status="checks_pending", ci_state="pending")),
        (Ledger(rounds=[{"round": 1, "state": "reserved"}]), _snapshot()),
    ):
        result, checked, observer, repairer = _run(ledger=ledger, snapshot=snapshot)
        assert result == {"status": "wait"}
        assert checked.reservation is None
        assert observer.calls == []
        assert repairer.calls == []


def test_remote_identity_change_after_push_is_uncertain_not_advanced():
    changed = Observer(_snapshot(NEW, author_id=777))
    result, ledger, _, _ = _run(observer=changed)
    assert result == {"status": "uncertain"}
    assert ledger.finished == [(URL, 1, "uncertain", "")]


def test_unconfirmed_or_failed_push_never_advances_head():
    for payload, expected in (
        ({"ok": False, "stage": "test"}, "failed"),
        ({"ok": False, "stage": "push"}, "uncertain"),
        ({"ok": False, "stage": "push_uncertain"}, "uncertain"),
        ({"ok": True, "stage": "pushed", "head_sha": "bad"}, "uncertain"),
    ):
        result, ledger, observer, _ = _run(repairer=Repairer(payload))
        assert result == {"status": expected}
        assert ledger.finished == [(URL, 1, expected, "")]
        assert observer.calls == [42]


def test_observation_error_after_push_remains_one_shot_uncertain():
    result, ledger, observer, _ = _run(observer=Observer(error=RuntimeError("secret")))
    assert result == {"status": "uncertain"}
    assert ledger.finished == [(URL, 1, "uncertain", "")]
    assert observer.calls == [42, 42]
    second, _, _, repairer = _run(ledger=ledger)
    assert second == {"status": "wait"}
    assert repairer.calls == []


def test_cancelled_long_repair_spends_round_and_never_retries():
    ledger = Ledger()
    observer = Observer()

    async def slow_repair(_branch, _sha):
        await asyncio.Event().wait()

    async def run():
        await asyncio.wait_for(repair_failed_ci_once(
            ledger, observer, slow_repair, run_id="run-42", pr_url=URL,
            snapshot=_snapshot(),
        ), timeout=0.01)

    try:
        asyncio.run(run())
    except TimeoutError:
        pass
    else:
        raise AssertionError("long repair did not reach its deadline")
    assert ledger.finished == [(URL, 1, "uncertain", "")]
    result, _, _, repairer = _run(ledger=ledger)
    assert result == {"status": "wait"}
    assert repairer.calls == []


def test_stale_caller_snapshot_cannot_reserve_a_repair():
    observer = Observer(before=_snapshot(NEW, status="checks_pending", ci_state="pending"))
    result, ledger, _, repairer = _run(observer=observer)
    assert result == {"status": "wait"}
    assert ledger.reservation is None
    assert repairer.calls == []
