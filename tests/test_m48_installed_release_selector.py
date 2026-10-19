"""A last-good release selection must survive restart without touching Git refs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger
from hive.core.installed_release import InstalledReleaseSelector


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _installed_releases(tmp_path: Path) -> tuple[Path, Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "config", "user.name", "Hive test")
    _git(source, "config", "user.email", "hive@example.invalid")
    (source / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (source / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "app.py")
    _git(source, "commit", "-qm", "release one")
    first = _git(source, "rev-parse", "HEAD")
    (source / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    _git(source, "commit", "-qam", "release two")
    second = _git(source, "rev-parse", "HEAD")
    release_root = tmp_path / "installed"
    (release_root / "releases").mkdir(parents=True)
    _git(source, "worktree", "add", "--detach", "-q", str(release_root / "releases" / first), first)
    _git(source, "worktree", "add", "--detach", "-q", str(release_root / "releases" / second), second)
    return source, release_root, first, second


def _verdict(ledger: DeployLedger, selector: InstalledReleaseSelector, *, sha: str, baseline: str, run_id: str,
             status: str, now: float, old_process: str, new_process: str) -> str:
    item = ledger.schedule(
        run_id, "gateway", "systemctl", "host-a", sha, baseline_sha=baseline,
        now=now, await_restart=True, baseline_process_id=old_process,
        exclusive=True, systemctl_scope="user",
    )
    selector.bind_deploy_receipt(item.id)
    assert ledger.confirm_gateway_start(
        "host-a", sha, new_process, now=now + 1, settling_seconds=0,
        systemctl_scope="user",
    ) is not None
    claimed = ledger.claim_due("host-a", "verifier", now=now + 60)
    assert claimed is not None
    ledger.finish(
        item.id, "verifier", status,
        failed_signals=() if status == HEALTHY else ("gateway",),
        now=now + 61, claim_count=claimed.claim_count,
    )
    return item.id


def test_verified_release_selection_survives_restart_and_preserves_git(tmp_path):
    source, release_root, first, second = _installed_releases(tmp_path)
    branch = _git(source, "symbolic-ref", "HEAD")
    branch_sha = _git(source, "rev-parse", "HEAD")
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")

    assert selector.select_candidate(first, expected_selected=None) == first
    healthy_id = _verdict(
        ledger, selector, sha=first, baseline="", run_id="run-good", status=HEALTHY,
        now=100, old_process="a" * 32, new_process="b" * 32,
    )
    assert selector.record_last_good(healthy_id) == first
    assert selector.select_candidate(second, expected_selected=first) == second
    failed_id = _verdict(
        ledger, selector, sha=second, baseline=first, run_id="run-bad", status=DEGRADED,
        now=200, old_process="b" * 32, new_process="c" * 32,
    )

    restarted = InstalledReleaseSelector(release_root, DeployLedger(tmp_path / "state.sqlite"),
                                         host_key="host-a", scope="user")
    assert restarted.selected_sha() == second
    assert restarted.last_good_sha() == first
    assert restarted.select_rollback_target(failed_id) == first
    assert restarted.selected_sha() == first
    assert restarted.select_rollback_target(failed_id) is None
    assert restarted.rollback_events() == (("run-bad", failed_id, second, first),)
    assert _git(source, "symbolic-ref", "HEAD") == branch
    assert _git(source, "rev-parse", "HEAD") == branch_sha


def test_unverified_or_dirty_release_cannot_be_selected(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    (release_root / "releases" / second / "app.py").write_text("VERSION = 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(second, expected_selected=first)
    assert selector.selected_sha() == first
    with pytest.raises(ValueError, match="selected release changed"):
        selector.select_candidate(first, expected_selected=second)


def test_legacy_healthy_receipt_cannot_become_last_good(tmp_path):
    _source, release_root, first, _second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    item = ledger.schedule("legacy", "gateway", "systemctl", "host-a", first, now=100)
    claim = ledger.claim_due("host-a", "verifier", now=130)
    ledger.finish(item.id, "verifier", HEALTHY, now=131, claim_count=claim.claim_count)
    with pytest.raises(ValueError, match="process-bound healthy receipt"):
        selector.record_last_good(item.id)
    assert selector.last_good_sha() is None


def test_ignored_release_artifact_blocks_selection(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    cache = release_root / "releases" / second / "__pycache__"
    cache.mkdir()
    (cache / "app.pyc").write_bytes(b"untrusted bytecode")
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(second, expected_selected=first)
    assert selector.selected_sha() == first


def test_untracked_release_file_blocks_selection_without_git_status(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    (release_root / "releases" / second / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(second, expected_selected=first)
    assert selector.selected_sha() == first


def test_demoted_last_good_cannot_be_selected_for_rollback(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    healthy_id = _verdict(
        ledger, selector, sha=first, baseline="", run_id="run-good", status=HEALTHY,
        now=100, old_process="a" * 32, new_process="b" * 32,
    )
    selector.record_last_good(healthy_id)
    selector.select_candidate(second, expected_selected=first)
    failed_id = _verdict(
        ledger, selector, sha=second, baseline=first, run_id="run-bad", status=DEGRADED,
        now=200, old_process="b" * 32, new_process="c" * 32,
    )
    ledger.mark_restart_failed(healthy_id, now=270)
    with pytest.raises(ValueError, match="no matching last-good release"):
        selector.select_rollback_target(failed_id)
    assert selector.selected_sha() == second


def test_release_checked_out_on_branch_is_not_an_installed_release(tmp_path):
    source, release_root, first, second = _installed_releases(tmp_path)
    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    _git(source, "-C", str(release_root / "releases" / second), "switch", "-q", "-c", "release-branch")
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(second, expected_selected=first)
    assert selector.selected_sha() == first


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_changed_release_bytes(tmp_path, index_flag):
    source, release_root, first, second = _installed_releases(tmp_path)
    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    release = release_root / "releases" / second
    _git(source, "-C", str(release), "update-index", index_flag, "app.py")
    (release / "app.py").write_text("VERSION = 999\n", encoding="utf-8")
    assert _git(release, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(second, expected_selected=first)
    assert selector.selected_sha() == first


def test_old_receipt_cannot_select_new_generation_of_same_sha(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    good = _verdict(
        ledger, selector, sha=first, baseline="", run_id="run-good", status=HEALTHY,
        now=100, old_process="a" * 32, new_process="b" * 32,
    )
    selector.record_last_good(good)
    selector.select_candidate(second, expected_selected=first)
    old_failed = _verdict(
        ledger, selector, sha=second, baseline=first, run_id="run-old-bad", status=DEGRADED,
        now=200, old_process="b" * 32, new_process="c" * 32,
    )
    selector.select_candidate(first, expected_selected=second)
    selector.select_candidate(second, expected_selected=first)
    new_pending = ledger.schedule(
        "run-new", "gateway", "systemctl", "host-a", second, baseline_sha=first,
        now=300, await_restart=True, baseline_process_id="b" * 32,
        exclusive=True, systemctl_scope="user",
    )
    selector.bind_deploy_receipt(new_pending.id)
    with pytest.raises(ValueError, match="generation"):
        selector.select_rollback_target(old_failed)
    assert selector.selected_sha() == second


def test_wrong_baseline_process_is_not_bound(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    good = _verdict(
        ledger, selector, sha=first, baseline="", run_id="run-good", status=HEALTHY,
        now=100, old_process="a" * 32, new_process="b" * 32,
    )
    selector.record_last_good(good)
    selector.select_candidate(second, expected_selected=first)
    pending = ledger.schedule(
        "run-bad-baseline", "gateway", "systemctl", "host-a", second,
        baseline_sha=first, now=200, await_restart=True,
        baseline_process_id="f" * 32, exclusive=True, systemctl_scope="user",
    )
    with pytest.raises(ValueError, match="baseline process"):
        selector.bind_deploy_receipt(pending.id)


def test_git_replace_cannot_change_release_tree_behind_commit_id(tmp_path):
    source, release_root, first, _second = _installed_releases(tmp_path)
    (source / "app.py").write_text("VERSION = 999\n", encoding="utf-8")
    _git(source, "commit", "-qam", "replacement content")
    replacement = _git(source, "rev-parse", "HEAD")
    _git(source, "replace", first, replacement)
    release = release_root / "releases" / first
    replacement_blob = _git(source, "rev-parse", f"{replacement}:app.py")
    _git(source, "-C", str(release), "update-index", "--cacheinfo", "100644", replacement_blob, "app.py")
    (release / "app.py").write_text("VERSION = 999\n", encoding="utf-8")
    assert _git(release, "rev-parse", "HEAD") == first
    assert _git(release, "ls-tree", "HEAD", "app.py") == _git(source, "ls-tree", replacement, "app.py")
    assert _git(release, "status", "--porcelain", "--untracked-files=all", "--ignored") == ""

    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    with pytest.raises(ValueError, match="clean installed release"):
        selector.select_candidate(first, expected_selected=None)
    assert selector.selected_sha() is None


def test_git_clean_filter_is_not_executed_during_release_verification(tmp_path):
    source, release_root, _first, second = _installed_releases(tmp_path)
    (source / ".git" / "info" / "attributes").write_text(
        "app.py filter=hiveprobe\n", encoding="utf-8",
    )
    _git(source, "config", "filter.hiveprobe.clean", "hive-nonexistent-filter-command")
    _git(source, "config", "filter.hiveprobe.required", "true")
    release = release_root / "releases" / second
    (release / "app.py").write_text("VERSION = 2\n", encoding="utf-8")

    selector = InstalledReleaseSelector(
        release_root, DeployLedger(tmp_path / "state.sqlite"),
        host_key="host-a", scope="user",
    )
    assert selector.select_candidate(second, expected_selected=None) == second


def test_receipt_cannot_first_bind_after_selection_returns_to_same_sha(tmp_path):
    _source, release_root, first, second = _installed_releases(tmp_path)
    ledger = DeployLedger(tmp_path / "state.sqlite")
    selector = InstalledReleaseSelector(release_root, ledger, host_key="host-a", scope="user")
    selector.select_candidate(first, expected_selected=None)
    good = _verdict(
        ledger, selector, sha=first, baseline="", run_id="run-good", status=HEALTHY,
        now=100, old_process="a" * 32, new_process="b" * 32,
    )
    selector.record_last_good(good)
    selector.select_candidate(second, expected_selected=first)
    stale = ledger.schedule(
        "run-stale", "gateway", "systemctl", "host-a", second, baseline_sha=first,
        now=200, await_restart=True, baseline_process_id="b" * 32,
        exclusive=True, systemctl_scope="user",
    )
    selector.select_candidate(first, expected_selected=second)
    selector.select_candidate(second, expected_selected=first)

    with pytest.raises(ValueError, match="generation"):
        selector.bind_deploy_receipt(stale.id)
