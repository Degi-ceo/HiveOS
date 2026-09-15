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
from pathlib import Path
from typing import TYPE_CHECKING

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

    def __init__(self, improver: SelfImprovement | None = None) -> None:
        self._improver = improver

    def bind(self, improver: SelfImprovement) -> None:
        """Attach the already configured self-improvement policy at runtime build."""
        self._improver = improver

    async def propose_file(
        self, *, path: str, expected_sha256: str, replacement: str,
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
