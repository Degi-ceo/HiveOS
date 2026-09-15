"""Bounded candidate-only write proposals for the coder specialist.

This module deliberately is not a filesystem tool.  It turns a declarative,
compare-and-swap file replacement into a REVIEW-tier ``Edit``.  The existing
``SelfImprovement`` + ``SelfModifier`` flow owns every real write: after an
out-of-band approval it creates an isolated candidate worktree, verifies the
candidate, tests it, scans it, and opens a reviewable PR.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from hive.agents.candidate_sandbox import CandidateContainerRunner, validate_candidate_argv
from hive.core.spec_search import Edit, EditOp, EditOutcome

if TYPE_CHECKING:
    from hive.core.spec_search import SelfImprovement


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REPLACEMENT_BYTES = 100_000
_ALLOWED_PREFIXES = ("src/", "tests/")


class CandidateBroker:
    """Queue compare-and-swap code proposals through the existing review flow.

    Until ``bind`` is called, requests fail closed.  The broker never exposes a
    candidate path or executes a command itself; its only side effect is the
    existing REVIEW-tier approval request created by ``SelfImprovement``.
    """

    def __init__(self, improver: SelfImprovement | None = None,
                 candidate_runner: CandidateContainerRunner | None = None,
                 audit: Callable[[dict], None] | None = None) -> None:
        self._improver = improver
        self._candidate_runner = candidate_runner
        self._audit = audit

    def bind(self, improver: SelfImprovement,
             candidate_runner: CandidateContainerRunner | None = None,
             audit: Callable[[dict], None] | None = None) -> None:
        """Attach the already configured self-improvement policy at runtime build."""
        self._improver = improver
        self._candidate_runner = candidate_runner
        self._audit = audit

    async def propose_file(
        self, *, path: str, expected_sha256: str, replacement: str,
        checks: Sequence[Sequence[str]] = (),
    ) -> EditOutcome | None:
        """Queue one full-file replacement, or reject it before any candidate exists."""
        if self._improver is None:
            return None
        normalized = _normalize_target(path)
        expected = str(expected_sha256).strip().casefold()
        if _SHA256_RE.fullmatch(expected) is None:
            return None
        if not isinstance(replacement, str):
            return None
        replacement_bytes = replacement.encode("utf-8")
        if len(replacement_bytes) > _MAX_REPLACEMENT_BYTES:
            return None
        if checks is None or isinstance(checks, (str, bytes)) or not isinstance(checks, Sequence):
            raise ValueError("candidate checks must be an argv sequence")
        if checks and self._candidate_runner is None:
            return None
        checked_argv = tuple(validate_candidate_argv(argv) for argv in checks)

        async def apply(candidate_worktree: str) -> list[str]:
            root = Path(candidate_worktree).resolve()
            target = root.joinpath(*normalized.split("/"))
            try:
                target.resolve().relative_to(root)
            except ValueError:
                return []
            # A leaf-only check is insufficient: ``src/`` or another
            # intermediate component can itself be a symlink.  Refuse before
            # the first read or write so even an in-root redirect cannot alter
            # an unintended candidate file.
            component = root
            for part in normalized.split("/"):
                component = component / part
                if component.is_symlink():
                    return []
            if not target.is_file():
                return []
            try:
                original = target.read_bytes()
            except OSError:
                return []
            if hashlib.sha256(original).hexdigest() != expected:
                return []
            try:
                original.decode("utf-8")
                target.write_bytes(replacement_bytes)
            except (OSError, UnicodeDecodeError):
                return []
            for argv in checked_argv:
                assert self._candidate_runner is not None
                rc, _output = await self._candidate_runner.run(candidate_worktree, argv)
                if self._audit is not None:
                    self._audit({"tool": "candidate_container_check", "status": "ok" if rc == 0 else "error",
                                 "args": {"command_kind": ":".join(argv[:3]),
                                          "argv_sha256": hashlib.sha256("\0".join(argv).encode()).hexdigest(),
                                          "image_reference_sha256": self._candidate_runner.image_reference_sha256}})
                if rc != 0:
                    target.write_bytes(original)
                    return []
            return [normalized]

        # The fixed metadata deliberately excludes the model-supplied content.
        # PATCH_CODE is deterministically REVIEW tier, independent of model input.
        edit = Edit(
            op=EditOp.PATCH_CODE,
            summary="coder candidate proposal",
            rationale="coder proposal requires independent review",
            apply=apply,
            target_files=[normalized],
            code=None,
            origin_source="coder_candidate_broker",
        )
        outcomes = await self._improver.run([edit])
        return outcomes[0] if outcomes else None


def _normalize_target(path: str) -> str:
    """Return a strictly repository-relative candidate target or raise ValueError."""
    raw = str(path).replace("\\", "/").strip()
    normalized = posixpath.normpath(raw)
    first = normalized.split("/", 1)[0]
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith(("../", "/"))
        or ":" in first
        or not normalized.startswith(_ALLOWED_PREFIXES)
    ):
        raise ValueError("candidate target is outside the permitted source/test roots")
    return normalized
