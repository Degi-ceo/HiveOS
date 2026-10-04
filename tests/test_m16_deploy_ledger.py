"""Focused contract tests for the standalone deployment ledger."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hive.core.deployment_ledger import DEGRADED, HEALTHY, PENDING, VERIFYING, DeployLedger

SHA_A = "a" * 40
SHA_B = "b" * 64


def _schedule(ledger: DeployLedger, *, host: str = "host-a", sha: str = SHA_A, now: float = 100):
    return ledger.schedule("run-141", "gateway", "systemctl", host, sha, now=now)


def test_schedule_settling_host_scope_and_restart(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    first = _schedule(ledger)
    second = _schedule(ledger, now=101)

    assert first.id != second.id
    assert first.status == first.state == PENDING
    assert first.verdict is None
    assert first.due_at == 130
    assert first.baseline_sha == ""
    assert DeployLedger(path).get(first.id) == first
    assert ledger.claim_due("host-b", "worker", now=130) is None
    assert ledger.claim_due("host-a", "worker", now=129.99) is None

    claimed = DeployLedger(path).claim_due("host-a", "worker", now=130)
    assert claimed.id == first.id
    assert claimed.status == VERIFYING
    assert claimed.claim_count == 1
    assert claimed.lease_until == 190
    assert ledger.get(second.id).status == PENDING


def test_finish_owner_lease_verdict_and_last_healthy_sha(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") is None
    first = _schedule(ledger)
    with pytest.raises(ValueError):
        ledger.finish(first.id, "worker", HEALTHY, now=130, claim_count=1)

    ledger.claim_due("host-a", "worker", now=130, lease_seconds=10)
    with pytest.raises(ValueError):
        ledger.finish(first.id, "intruder", HEALTHY, now=131, claim_count=1)
    with pytest.raises(ValueError):
        ledger.finish(first.id, "worker", HEALTHY, now=140, claim_count=1)
    with pytest.raises(ValueError):
        ledger.finish(first.id, "worker", DEGRADED, now=131, claim_count=1)
    assert ledger.get(first.id).status == VERIFYING

    healthy = ledger.finish(first.id, "worker", HEALTHY, now=131, claim_count=1)
    assert healthy.verdict == HEALTHY
    assert healthy.failed_signals == ()
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") == SHA_A
    assert ledger.last_healthy_sha("host-b", "gateway", "systemctl") is None
    assert ledger.last_healthy_sha("host-a", "gateway", "docker") is None
    with pytest.raises(ValueError):
        ledger.finish(first.id, "worker", HEALTHY, now=132, claim_count=1)

    second = _schedule(ledger, sha=SHA_B, now=200)
    ledger.claim_due("host-a", "worker-2", now=230)
    degraded = ledger.finish(
        second.id, "worker-2", DEGRADED, ("doctor", "smoke", "doctor"),
        now=231, claim_count=1,
    )
    assert degraded.failed_signals == ("doctor", "smoke")
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") == SHA_A


def test_last_healthy_sha_uses_deploy_order_not_completion_order(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    older = _schedule(ledger, sha=SHA_A, now=100)
    newer = _schedule(ledger, sha=SHA_B, now=101)
    first_claim = ledger.claim_due("host-a", "older-worker", now=131)
    second_claim = ledger.claim_due("host-a", "newer-worker", now=131)
    assert first_claim.id == older.id
    assert second_claim.id == newer.id
    ledger.finish(newer.id, "newer-worker", HEALTHY, now=132, claim_count=1)
    ledger.finish(older.id, "older-worker", HEALTHY, now=133, claim_count=1)
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") == SHA_B


def test_last_healthy_sha_uses_insert_order_when_timestamps_tie(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    older = _schedule(ledger, sha=SHA_A, now=100)
    newer = _schedule(ledger, sha=SHA_B, now=100)
    assert ledger.claim_due("host-a", "older-worker", now=130).id == older.id
    assert ledger.claim_due("host-a", "newer-worker", now=130).id == newer.id
    ledger.finish(newer.id, "newer-worker", HEALTHY, now=131, claim_count=1)
    ledger.finish(older.id, "older-worker", HEALTHY, now=132, claim_count=1)
    assert ledger.last_healthy_sha("host-a", "gateway", "systemctl") == SHA_B


def test_expired_verification_claim_has_one_retry_then_degrades(tmp_path):
    path = tmp_path / "deploy.sqlite"
    first = DeployLedger(path)
    item = _schedule(first)
    original = first.claim_due("host-a", "worker-1", now=130, lease_seconds=5)
    assert original.claim_count == 1
    assert first.claim_due("host-a", "worker-2", now=134.99) is None

    reopened = DeployLedger(path)
    retry = reopened.claim_due("host-a", "worker-2", now=135, lease_seconds=5)
    assert retry.id == item.id
    assert retry.claim_count == 2
    assert retry.owner == "worker-2"
    with pytest.raises(ValueError):
        first.finish(item.id, "worker-1", HEALTHY, now=136, claim_count=1)
    assert reopened.claim_due("host-b", "worker-3", now=140) is None
    assert reopened.get(item.id).status == VERIFYING
    assert reopened.claim_due("host-a", "worker-3", now=140) is None

    exhausted = first.get(item.id)
    assert exhausted.status == DEGRADED
    assert exhausted.verdict == DEGRADED
    assert exhausted.failed_signals == ("timeout",)
    assert exhausted.claim_count == 2
    assert exhausted.completed_at == 140
    with pytest.raises(ValueError):
        reopened.finish(item.id, "worker-2", HEALTHY, now=140, claim_count=2)


def test_alert_scan_degrades_exhausted_claim_after_verifier_crash(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    item = _schedule(ledger)
    ledger.claim_due("host-a", "worker-1", now=130, lease_seconds=5)
    ledger.claim_due("host-a", "worker-2", now=135, lease_seconds=5)

    recovered = DeployLedger(path).claim_alert("host-a", "alerter", now=140)
    assert recovered.id == item.id
    assert recovered.failed_signals == ("timeout",)
    assert recovered.status == DEGRADED
    assert recovered.completed_at == 140


def test_atomic_claims_across_connections(tmp_path):
    path = tmp_path / "deploy.sqlite"
    item = _schedule(DeployLedger(path))
    barrier = threading.Barrier(2)
    claims = []
    errors = []

    def worker(owner):
        try:
            barrier.wait(timeout=5)
            claims.append(DeployLedger(path).claim_due("host-a", owner, now=130))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("worker-1", "worker-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len([claim for claim in claims if claim is not None]) == 1
    assert DeployLedger(path).get(item.id).claim_count == 1


def test_restart_failure_and_alert_lease_retry_are_durable(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    item = _schedule(ledger)
    failed = ledger.mark_restart_failed(item.id, now=102)
    assert failed.status == DEGRADED
    assert failed.failed_signals == ("restart",)
    assert ledger.mark_restart_failed(item.id, now=103) == failed
    assert ledger.claim_due("host-a", "worker", now=130) is None

    reopened = DeployLedger(path)
    claim = reopened.claim_alert("host-a", "alerter-1", now=104, lease_seconds=5)
    assert claim.id == item.id
    assert claim.alert_lease_until == 109
    assert ledger.claim_alert("host-a", "alerter-2", now=108) is None
    with pytest.raises(ValueError):
        reopened.mark_alert_sent(item.id, "alerter-2", now=105, claim_count=claim.alert_claim_count)
    with pytest.raises(ValueError):
        reopened.mark_alert_sent(item.id, "alerter-1", now=109, claim_count=claim.alert_claim_count)

    retried = ledger.claim_alert("host-a", "alerter-2", now=109)
    assert retried.id == item.id
    with pytest.raises(ValueError):
        reopened.mark_alert_sent(item.id, "alerter-1", now=110, claim_count=claim.alert_claim_count)
    sent = ledger.mark_alert_sent(item.id, "alerter-2", now=110,
                                  claim_count=retried.alert_claim_count)
    assert sent.alert_sent_at == 110
    assert DeployLedger(path).mark_alert_sent(
        item.id, "alerter-2", now=999, claim_count=retried.alert_claim_count,
    ) == sent
    assert reopened.claim_alert("host-a", "alerter-3", now=999) is None


def test_stale_alert_claim_cannot_mark_sent_after_same_owner_reclaims(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    item = _schedule(ledger)
    ledger.mark_restart_failed(item.id, now=101)
    first = ledger.claim_alert("host-a", "same-owner", now=102, lease_seconds=5)
    second = DeployLedger(path).claim_alert("host-a", "same-owner", now=107, lease_seconds=5)
    assert first.alert_claim_count == 1
    assert second.alert_claim_count == 2
    with pytest.raises(ValueError, match="stale"):
        ledger.mark_alert_sent(item.id, "same-owner", now=108,
                               claim_count=first.alert_claim_count)
    assert ledger.get(item.id).alert_sent_at is None
    assert ledger.mark_alert_sent(item.id, "same-owner", now=108,
                                  claim_count=second.alert_claim_count).alert_sent_at == 108


def test_existing_ledger_schema_gains_alert_claim_generation(tmp_path):
    path = tmp_path / "deploy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE deploy_ledger (id TEXT PRIMARY KEY, host_key TEXT, "
            "target TEXT, mode TEXT, status TEXT, created_at REAL, due_at REAL, "
            "lease_until REAL, completed_at REAL, alert_sent_at REAL, alert_lease_until REAL)"
        )
    DeployLedger(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(deploy_ledger)")}
    assert "alert_claim_count" in columns


def test_alert_claims_are_atomic_across_connections(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    item = _schedule(ledger)
    ledger.mark_restart_failed(item.id, now=101)
    barrier = threading.Barrier(2)
    claims = []

    def claim(owner):
        barrier.wait(timeout=5)
        claims.append(DeployLedger(path).claim_alert("host-a", owner, now=102))

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len([record for record in claims if record is not None]) == 1


def test_alert_claim_cannot_take_remote_host_work_or_expire_its_claim(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    remote = _schedule(ledger, host="host-b")
    ledger.claim_due("host-b", "remote-worker-1", now=130, lease_seconds=5)
    ledger.claim_due("host-b", "remote-worker-2", now=135, lease_seconds=5)
    assert ledger.claim_alert("host-a", "local-alerter", now=140) is None
    assert ledger.get(remote.id).status == VERIFYING
    claimed = ledger.claim_alert("host-b", "remote-alerter", now=140)
    assert claimed.id == remote.id
    assert claimed.failed_signals == ("timeout",)


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("run_id", "please run this prompt"),
        ("run_id", "token=secret"),
        ("host_key", "host\nsecret"),
        ("target", "database"),
        ("mode", "shell"),
        ("expected_sha", "a" * 39),
        ("expected_sha", "g" * 40),
        ("baseline_sha", "not-a-sha"),
        ("settling_seconds", -1),
        ("settling_seconds", 3601),
        ("settling_seconds", float("inf")),
        ("settling_seconds", 10**1000),
        ("now", float("nan")),
        ("now", 10**1000),
        ("target", []),
    ],
)
def test_schedule_rejects_unsafe_or_unbounded_fields(tmp_path, change, value):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    fields = dict(
        run_id="run-141",
        target="gateway",
        mode="systemctl",
        host_key="host-a",
        expected_sha=SHA_A,
        baseline_sha="",
        now=100,
        settling_seconds=30,
    )
    fields[change] = value
    with pytest.raises(ValueError):
        ledger.schedule(**fields)
    with sqlite3.connect(tmp_path / "deploy.sqlite") as db:
        assert db.execute("SELECT count(*) FROM deploy_ledger").fetchone()[0] == 0


def test_redaction_by_rejection_and_bounded_codes(tmp_path):
    path = tmp_path / "deploy.sqlite"
    ledger = DeployLedger(path)
    item = ledger.schedule(
        "run-141", "keeper", "ssh", "host-a", SHA_B.upper(), SHA_A.upper(), now=0, settling_seconds=0
    )
    assert item.expected_sha == SHA_B
    assert item.baseline_sha == SHA_A
    ledger.claim_due("host-a", "worker", now=0)
    with pytest.raises(ValueError):
        ledger.finish(item.id, "worker", DEGRADED, ("doctor: token=secret",), now=1,
                      claim_count=1)
    with pytest.raises(ValueError):
        ledger.finish(item.id, "worker", HEALTHY, ("doctor",), now=1, claim_count=1)
    with pytest.raises(ValueError):
        ledger.claim_due("host-a", "worker", now=1, lease_seconds=float("inf"))
    result = ledger.finish(item.id, "worker", DEGRADED, ("doctor", "revision"),
                           now=1, claim_count=1)
    assert result.failed_signals == ("doctor", "revision")
    assert "secret" not in path.read_bytes().decode("latin1")


def test_mark_restart_failed_preempts_active_claim_and_rejects_healthy(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _schedule(ledger)
    ledger.claim_due("host-a", "worker", now=130)
    assert ledger.mark_restart_failed(item.id, now=131).status == DEGRADED
    with pytest.raises(ValueError):
        ledger.finish(item.id, "worker", HEALTHY, now=132, claim_count=1)
    healthy = _schedule(ledger, now=200)
    ledger.claim_due("host-a", "worker", now=230)
    ledger.finish(healthy.id, "worker", HEALTHY, now=231, claim_count=1)
    with pytest.raises(ValueError):
        ledger.mark_restart_failed(healthy.id, now=232)


def test_stale_worker_cannot_finish_reclaimed_lease_with_same_owner(tmp_path):
    ledger = DeployLedger(tmp_path / "deploy.sqlite")
    item = _schedule(ledger)
    first = ledger.claim_due("host-a", "same-owner", now=130, lease_seconds=5)
    second = ledger.claim_due("host-a", "same-owner", now=135, lease_seconds=5)
    assert first.claim_count == 1 and second.claim_count == 2
    with pytest.raises(ValueError):
        ledger.finish(item.id, "same-owner", HEALTHY, now=136,
                      claim_count=first.claim_count)
    assert ledger.get(item.id).status == VERIFYING
    assert ledger.finish(item.id, "same-owner", HEALTHY, now=136,
                         claim_count=second.claim_count).status == HEALTHY
