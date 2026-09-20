"""Docker execution boundary for credential-free specialist workers.

The supervisor retains model credentials, tool instances, approvals and state.
This module contains only the untrusted worker loop in a disposable, networkless
container with a read-only source mount.  It is deliberately separate from the
self-modification runner: workers use stdio IPC and never need a writable repo.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    """Validate static prerequisites without pulling or running an image."""
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


class DockerWorkerSandbox:
    """Build an exec-safe Docker command for one supervised worker process."""

    def __init__(self, image: str, source_root: str | Path) -> None:
        capability = worker_sandbox_capability(image)
        if not capability.available:
            raise WorkerSandboxUnavailable(capability.detail)
        self.image = image
        self.source_root = Path(source_root).resolve()
        self.container_name = ""

    def wrap(self, worker_args: tuple[str, ...]) -> tuple[str, ...]:
        """Return a no-network, no-write, no-capability container command.

        ``worker_args`` is the existing Python worker entrypoint.  Its source
        root argument is replaced with the fixed in-container read-only path.
        The caller uses exec (never a host shell), so all values remain argv.
        """
        if not worker_args:
            raise ValueError("worker command is empty")
        container_name = f"hive-worker-{uuid.uuid4().hex}"
        self.container_name = container_name
        args = list(worker_args)
        if args:
            args[0] = "python"
        if args and args[-1] == str(self.source_root):
            args[-1] = "/app"
        return (
            "docker", "run", "--rm", "--name", container_name, "--interactive",
            "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "512m", "--cpus", "1",
            "--user", "65534:65534", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--volume", f"{self.source_root}:/app:ro", "--workdir", "/app",
            self.image, *args,
        )

    async def cleanup(self) -> None:
        """Force-remove a daemon-owned container if its Docker client died."""
        if not self.container_name:
            return
        name, self.container_name = self.container_name, ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=without_privileged_credentials(),
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (OSError, TimeoutError):
            return


__all__ = [
    "DockerWorkerSandbox", "WORKER_SANDBOX_MODES", "WorkerSandboxCapability",
    "WorkerSandboxUnavailable", "worker_sandbox_capability",
]
