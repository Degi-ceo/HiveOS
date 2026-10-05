"""Fail-closed container execution primitive for future coder candidate checks.

This is deliberately separate from the general self-modification sandbox.  It
accepts only typed, read-only diagnostic commands and always invokes Docker by
``exec`` argv; it never builds a shell command or falls back to the host.
"""
from __future__ import annotations

import asyncio
import hashlib
import posixpath
import re
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path

from hive.core.child_env import without_privileged_credentials

_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}$")
_PINNED_IMAGE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}@(sha256:[0-9a-f]{64})$"
)
_MAX_ARGV = 12
_MAX_ARG_BYTES = 240
_COMMAND_TIMEOUT_SECONDS = 60.0
_CLEANUP_TIMEOUT_SECONDS = 5.0
_MAX_OUTPUT_BYTES = 8_192
_SAFE_PATH_PREFIXES = ("src/", "tests/")
_Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def _remove_named_container(argv: list[str]) -> bool:
    """Best-effort cleanup after a Docker client timeout or cancellation."""
    if "--name" not in argv:
        return True
    try:
        name = argv[argv.index("--name") + 1]
    except IndexError:
        return False
    try:
        cleanup = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=without_privileged_credentials(),
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            cleanup.kill()
            try:
                await asyncio.wait_for(cleanup.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except TimeoutError:
                return False
        return True
    except OSError:
        return False


async def _default_run(argv: list[str]) -> tuple[int, str]:
    """Exec Docker directly with a credential-filtered environment."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=without_privileged_credentials(),
    )
    captured = bytearray()

    async def drain(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while chunk := await stream.read(4_096):
            if len(captured) < _MAX_OUTPUT_BYTES:
                captured.extend(chunk[:_MAX_OUTPUT_BYTES - len(captured)])

    started = time.monotonic()
    readers = [asyncio.create_task(drain(proc.stdout)), asyncio.create_task(drain(proc.stderr))]
    try:
        await asyncio.wait_for(proc.wait(), timeout=_COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            return 125, "[candidate command cleanup failed]"
        if not await _remove_named_container(argv):
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            return 125, "[candidate command cleanup failed]"
        await asyncio.gather(*readers)
        return 124, "[candidate command timed out]"
    except asyncio.CancelledError:
        # Killing the Docker client alone can leave its named container alive.
        # Reap both, then propagate cancellation so the issuer cannot emit a
        # receipt for an interrupted diagnostic.
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        await _remove_named_container(argv)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    await asyncio.gather(*readers)
    elapsed = time.monotonic() - started
    if proc.returncode == 0:
        return 0, f"[candidate command succeeded in {elapsed:.2f}s]"
    return int(proc.returncode or 1), "[candidate command failed; output suppressed]"


def _safe_candidate_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/").strip())
    first = normalized.split("/", 1)[0]
    if (not normalized or normalized in {".", ".."}
            or normalized.startswith(("../", "/")) or ":" in first
            or normalized not in {"src", "tests"}
            and not normalized.startswith(_SAFE_PATH_PREFIXES)):
        raise ValueError("candidate command path is outside src/ or tests/")
    return normalized


def validate_candidate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Validate one narrow, read-only candidate diagnostic command.

    The allowed shapes deliberately do not include a shell, an interpreter
    expression, package installation, Git mutation, or configuration override.
    """
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise ValueError("candidate command must be an argv sequence")
    parts = tuple(argv)
    if not 3 <= len(parts) <= _MAX_ARGV or any(not isinstance(p, str) for p in parts):
        raise ValueError("candidate command has invalid argv")
    if any("\x00" in p or len(p.encode("utf-8")) > _MAX_ARG_BYTES for p in parts):
        raise ValueError("candidate command has unsafe argument bytes")

    if parts[:3] == ("python", "-m", "pytest"):
        rest = parts[3:]
        if not rest or any(p in {"-c", "--config", "-o", "--rootdir"} for p in rest):
            raise ValueError("candidate pytest arguments are not permitted")
        paths = [p for p in rest if not p.startswith("-")]
        if not paths:
            raise ValueError("candidate pytest requires only safe source/test paths")
        for path in paths:
            _safe_candidate_path(path)
        if any(p not in {"-q", "--disable-warnings", "-p", "no:cacheprovider", *paths}
               for p in rest):
            raise ValueError("candidate pytest option is not permitted")
        return parts

    if parts[:3] == ("python", "-m", "compileall"):
        if len(parts) != 4:
            raise ValueError("candidate compileall requires one safe source/test path")
        _safe_candidate_path(parts[3])
        return parts

    if parts[:2] == ("ruff", "check"):
        if len(parts) != 3:
            raise ValueError("candidate ruff requires one safe source/test path")
        _safe_candidate_path(parts[2])
        return parts
    raise ValueError("candidate command is not allowlisted")


class CandidateContainerRunner:
    """Run a validated candidate diagnostic in a locked-down Docker container."""

    def __init__(self, image: str, *, run: _Runner | None = None) -> None:
        if not image or _IMAGE_RE.fullmatch(image) is None:
            raise ValueError("candidate sandbox image is required and must be valid")
        self._image = image
        self._run = run or _default_run

    @property
    def image_reference_sha256(self) -> str:
        return hashlib.sha256(self._image.encode("utf-8")).hexdigest()

    @property
    def pinned_image_digest(self) -> str:
        """Return the immutable image identity required for evidence receipts.

        Ordinary candidate checks retain their existing configuration semantics.
        A future evidence issuer is stricter: a mutable Docker tag cannot attest
        which filesystem or interpreter executed a diagnostic.
        """
        matched = _PINNED_IMAGE_RE.fullmatch(self._image)
        if matched is None:
            raise ValueError("candidate evidence requires an image pinned by sha256 digest")
        return matched.group(1)

    def docker_argv(
        self, candidate_worktree: str, argv: Sequence[str], *, pull_never: bool = False,
    ) -> list[str]:
        validated = validate_candidate_argv(argv)
        declared_root = Path(candidate_worktree)
        if declared_root.is_symlink() or not declared_root.is_dir():
            raise ValueError("candidate worktree is unavailable")
        root = declared_root.resolve()
        command = [
            "docker", "run", "--rm", "--name", f"hive-candidate-{uuid.uuid4().hex}",
            "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "512m",
            "--cpus", "1", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin", "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{root}:/repo:ro", "-w", "/repo",
            self._image, *validated,
        ]
        if pull_never:
            command[2:2] = ["--pull", "never"]
        return command

    async def run(self, candidate_worktree: str, argv: Sequence[str]) -> tuple[int, str]:
        """Execute only through Docker; unavailable Docker is a denied operation."""
        try:
            return await self._run(self.docker_argv(candidate_worktree, argv))
        except (OSError, asyncio.TimeoutError):
            return 126, "[candidate container unavailable]"

    async def run_pinned_evidence(
        self, candidate_worktree: str, argv: Sequence[str],
    ) -> tuple[int, str]:
        """Run a receipt diagnostic with an immutable local image only.

        This is intentionally separate from ``run`` so existing review-bound
        candidate checks do not silently gain an attestation meaning.
        """
        self.pinned_image_digest
        try:
            return await self._run(self.docker_argv(candidate_worktree, argv, pull_never=True))
        except (OSError, asyncio.TimeoutError):
            return 126, "[candidate container unavailable]"
