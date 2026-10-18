"""Host-owned, fixed-command Docker runner for M36 candidate evidence.

The Docker CLI receives only an allowlisted launch environment. The candidate
checkout is mounted read-only into a digest-pinned, network-isolated Linux
container; diagnostics may write only to an ephemeral tmpfs. No output from
candidate-controlled code is returned to the host approval path.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from pathlib import Path

from hive.core.candidate_evidence import _CHECKS
from hive.core.child_env import minimal_worker_environment

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}@sha256:[0-9a-f]{64}\Z")
_CHECKOUT = re.compile(r"hive-supervisor-evidence-[0-9a-f]{32}\Z")
_ALLOWED = frozenset(argv for _name, argv in _CHECKS)
_CLEANUP_TIMEOUT = 10.0


async def _run_command(argv: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """Execute Docker without a shell, inherited credentials or raw output."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=minimal_worker_environment(),
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_TIMEOUT)
            except (OSError, TimeoutError):
                pass
        raise
    if proc.returncode is None:
        raise RuntimeError("Docker client ended without a return code")
    return proc.returncode, ""


class DockerPinnedEvidenceRunner:
    """Run only M36's fixed diagnostics in one disposable pinned container each."""

    def __init__(
        self, *, image: str, repo_root: str | Path, timeout_seconds: float = 900.0,
    ) -> None:
        if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
            raise ValueError("candidate evidence requires a digest-pinned Docker image")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValueError("invalid candidate evidence timeout")
        if not 1 <= timeout_seconds <= 1800:
            raise ValueError("candidate evidence timeout must be between 1 and 1800 seconds")
        self._root = Path(repo_root).resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("candidate evidence repository must be a directory")
        self._image = image
        self._digest = image.rsplit("@", 1)[1]
        self._timeout = float(timeout_seconds)

    @property
    def pinned_image_digest(self) -> str:
        return self._digest

    def _allowed_checkout(self, candidate_worktree: str) -> Path | None:
        try:
            if not isinstance(candidate_worktree, str):
                return None
            checkout = Path(candidate_worktree).resolve(strict=True)
            parent = self._root / ".worktrees"
            if (
                not checkout.is_dir() or not parent.is_dir() or parent.is_symlink()
                or parent.resolve(strict=True).parent != self._root
                or checkout.parent != parent.resolve(strict=True)
                or _CHECKOUT.fullmatch(checkout.name) is None
                or any(char in str(checkout) for char in (",", "\n", "\r"))
            ):
                return None
            return checkout
        except (OSError, ValueError, RuntimeError):
            return None

    async def _cleanup(self, name: str) -> None:
        try:
            await _run_command(
                ["docker", "rm", "-f", name], str(self._root), _CLEANUP_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - cleanup uncertainty still rejects evidence
            pass

    async def run_pinned_evidence(
        self, candidate_worktree: str, argv: tuple[str, ...],
    ) -> tuple[int, str]:
        """Return success only for one fixed command in an isolated checkout."""
        checkout = self._allowed_checkout(candidate_worktree)
        if checkout is None or argv not in _ALLOWED:
            return 1, ""
        name = f"hive-evidence-{uuid.uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--pull=never", "--no-healthcheck",
            "--log-driver=none", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534",
            "--pids-limit=128", "--memory=2g", "--cpus=2",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
            "--env=HOME=/tmp", "--env=TMPDIR=/tmp",
            "--env=PYTHONPYCACHEPREFIX=/tmp/pycache",
            "--env=PYTEST_ADDOPTS=-p no:cacheprovider",
            "--mount", f"type=bind,src={checkout},dst=/repo,readonly",
            "--workdir=/repo", f"--entrypoint={argv[0]}",
            self._image, *argv[1:],
        ]
        try:
            rc, _ignored_output = await _run_command(command, str(self._root), self._timeout)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup(name))
            raise
        except Exception:  # noqa: BLE001 - Docker uncertainty cannot issue evidence
            await self._cleanup(name)
            return 1, ""
        return rc, ""
