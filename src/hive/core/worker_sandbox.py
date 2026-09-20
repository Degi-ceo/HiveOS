"""Policy-neutral Docker capability check for the worker compute boundary."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from hive.core.child_env import without_privileged_credentials

WORKER_SANDBOX_MODES = frozenset({"off", "preferred", "required"})
_DIGEST_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")


class WorkerSandboxUnavailable(RuntimeError):
    """Raised when a required Docker worker boundary cannot be constructed."""


@dataclass(frozen=True, slots=True)
class WorkerSandboxCapability:
    available: bool
    detail: str


def worker_sandbox_capability(image: str, *, verify_runtime: bool = False) -> WorkerSandboxCapability:
    """Check only Docker availability and a pre-pulled, pinned image."""
    if not image:
        return WorkerSandboxCapability(False, "HIVE_WORKER_SANDBOX_IMAGE is empty")
    if _DIGEST_IMAGE.fullmatch(image) is None:
        return WorkerSandboxCapability(False, "HIVE_WORKER_SANDBOX_IMAGE must be digest-pinned")
    if shutil.which("docker") is None:
        return WorkerSandboxCapability(False, "docker binary is not available")
    if verify_runtime:
        env = without_privileged_credentials()
        for argv, detail in (
            (("docker", "info", "--format", "{{.ID}}"), "Docker daemon is unavailable"),
            (("docker", "image", "inspect", "--format", "{{.Id}}", image),
             "digest-pinned worker image is not available locally"),
        ):
            try:
                completed = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env, timeout=5.0, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return WorkerSandboxCapability(False, detail)
            if completed.returncode != 0:
                return WorkerSandboxCapability(False, detail)
    return WorkerSandboxCapability(True, "digest-pinned Docker worker sandbox available")
