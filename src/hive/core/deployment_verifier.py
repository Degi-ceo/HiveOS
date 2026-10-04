"""Bounded post-deploy verdict worker for the durable deployment ledger.

Probe implementations are injected by the trusted host runtime. This worker
never executes a shell command, reads a secret, restarts a service, or sends an
alert; those transport boundaries are wired separately.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

from hive.core.child_env import minimal_worker_environment
from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger, DeployRecord

HealthProbe = Callable[[DeployRecord], Awaitable[bool]]
RevisionProbe = Callable[[DeployRecord], Awaitable[str | None]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class LocalGatewayHealth:
    """Read only the local unauthenticated health endpoint, never a redirect."""

    def __init__(self, url: str, *, timeout_seconds: float = 3.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path != "/health" or parsed.query or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("gateway health URL must be local HTTP /health")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 10
        ):
            raise ValueError("gateway timeout must be between 0 and 10 seconds")
        self._url = url
        self._timeout = float(timeout_seconds)
        self._cached: dict[tuple[str, int], dict] = {}

    def _fetch(self) -> dict:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )
        request = urllib.request.Request(self._url, method="GET")
        try:
            with opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    return {}
                payload = response.read(4097)
        except (OSError, urllib.error.URLError, ValueError):
            return {}
        if len(payload) > 4096:
            return {}
        try:
            data = json.loads(payload)
        except (UnicodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def gateway(self, record: DeployRecord) -> bool:
        data = await asyncio.to_thread(self._fetch)
        if len(self._cached) >= 16:
            self._cached.clear()
        self._cached[(record.id, record.claim_count)] = data
        return data.get("status") == "ok" and data.get("service") == "hiveos-gateway"

    async def revision(self, record: DeployRecord) -> str | None:
        data = self._cached.pop((record.id, record.claim_count), None)
        if data is None:
            data = await asyncio.to_thread(self._fetch)
        value = data.get("source_revision")
        return value if isinstance(value, str) else None


async def local_doctor_probe(_record: DeployRecord) -> bool:
    """Run existing doctor checks without rendering secret-bearing details."""
    from hive.core import doctor

    def check() -> bool:
        rows = doctor.check(fix=False)
        return bool(rows) and all(
            passed or any(warning in name for warning in doctor._WARN_ONLY)
            for name, passed, _detail in rows
        )

    return await asyncio.to_thread(check)


async def deterministic_smoke_probe(record: DeployRecord, *, repo_root: Path) -> bool:
    """Run the bundled real-runtime smoke dataset without inherited secrets."""
    del record
    root = repo_root.resolve(strict=True)
    dataset = root / "evals" / "datasets" / "runtime_smoke.jsonl"
    if not dataset.is_file() or dataset.is_symlink():
        return False
    env = minimal_worker_environment()
    env["PYTHONPATH"] = str(root / "src")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "hive.evals.cli", "run", str(dataset),
            "--target", "hive-runtime", "--quiet",
            cwd=root, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await proc.wait() == 0
    except asyncio.CancelledError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await asyncio.shield(proc.wait())
        raise


class DeploymentVerifier:
    """Claim one locally owned due receipt and store a safe, final verdict."""

    def __init__(
        self,
        ledger: DeployLedger,
        *,
        host_key: str,
        owner: str,
        doctor: HealthProbe,
        gateway: HealthProbe,
        smoke: HealthProbe,
        revision: RevisionProbe,
        probe_timeout_seconds: float = 10.0,
    ) -> None:
        if not all(callable(probe) for probe in (doctor, gateway, smoke, revision)):
            raise ValueError("all deployment probes must be callable")
        if (
            isinstance(probe_timeout_seconds, bool)
            or not isinstance(probe_timeout_seconds, (int, float))
            or not math.isfinite(probe_timeout_seconds)
            or not 0 < probe_timeout_seconds <= 600
        ):
            raise ValueError("probe timeout must be between 0 and 600 seconds")
        self._ledger = ledger
        self._host_key = host_key
        self._owner = owner
        self._doctor = doctor
        self._gateway = gateway
        self._smoke = smoke
        self._revision = revision
        self._timeout = float(probe_timeout_seconds)

    def next_incident(self) -> DeployRecord | None:
        return self._ledger.next_degraded_without_incident(self._host_key)

    def mark_incident_recorded(self, id: str) -> DeployRecord:
        return self._ledger.mark_incident_recorded(id, self._host_key)

    async def verify_due(self) -> DeployRecord | None:
        """Verify one due receipt; a cancelled worker leaves its lease to expire.

        The ledger alone determines host ownership, claim generation, and the
        bounded retry count. Only allowlisted signal codes are persisted.
        """
        record = self._ledger.claim_due(
            self._host_key, self._owner,
            lease_seconds=4 * self._timeout + 30,
        )
        if record is None:
            return None
        failed: list[str] = []
        for signal, probe in (
            ("doctor", self._doctor),
            ("gateway", self._gateway),
            ("smoke", self._smoke),
        ):
            try:
                passed = await asyncio.wait_for(probe(record), timeout=self._timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                failed.extend((signal, "timeout"))
            except Exception:  # noqa: BLE001 - raw probe error must not be persisted
                failed.append(signal)
            else:
                if passed is not True:
                    failed.append(signal)
        try:
            observed = await asyncio.wait_for(self._revision(record), timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failed.extend(("revision", "timeout"))
        except Exception:  # noqa: BLE001 - raw probe error must not be persisted
            failed.append("revision")
        else:
            if not isinstance(observed, str) or observed.lower() != record.expected_sha:
                failed.append("revision")
        signals = tuple(dict.fromkeys(failed))
        return self._ledger.finish(
            record.id, self._owner, DEGRADED if signals else HEALTHY,
            failed_signals=signals, claim_count=record.claim_count,
        )
