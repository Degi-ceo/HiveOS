"""Preliminary manual-tier policy for retained existing-PR code repairs.

This is not an evaluation verdict or permission to publish. SelfModifier must
first complete its sandboxed tests and secret/path checks; M36 host diagnostics
and an out-of-band approver decision are still required before M42 can push.
"""

from __future__ import annotations

import re
import uuid

LEGACY_POLICY_VERSION = "m41-self-mod-v1"
SUPERVISED_POLICY_VERSION = "m44-supervised-manual-v1"
ALLOWED_POLICY_VERSIONS = frozenset({LEGACY_POLICY_VERSION, SUPERVISED_POLICY_VERSION})

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_EVALUATION_CONTROL_PREFIXES = (
    "evals/", "src/hive/evals/", "src/hive/core/learning/",
)
_EVALUATION_CONTROL_EXACT = frozenset({
    "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py",
    "sitecustomize.py", "usercustomize.py", "src/sitecustomize.py",
    "src/usercustomize.py", "src/hive/__init__.py",
})


def is_evaluation_control(path: str) -> bool:
    """Reject candidate changes to evaluator controls even for human review."""
    normalized = path.replace("\\", "/").casefold()
    return (
        normalized in _EVALUATION_CONTROL_EXACT
        or normalized.endswith(("/conftest.py", "/sitecustomize.py", "/usercustomize.py"))
        or any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in _EVALUATION_CONTROL_PREFIXES
        )
    )


def _one_code_path(changed: list[str]) -> bool:
    if not isinstance(changed, list) or len(changed) != 1:
        return False
    path = changed[0]
    if not isinstance(path, str) or "\\" in path or not path.endswith(".py"):
        return False
    parts = path.split("/")
    if (
        (parts[:2] != ["src", "hive"] and parts[0] != "tests")
        or len(parts) < (3 if parts[0] == "src" else 2)
        or any(part in (".", "..") or _PATH_PART.fullmatch(part) is None for part in parts)
    ):
        return False
    return not is_evaluation_control(path)


async def qualify_supervised_review_candidate(
    worktree: str, base_commit: str, changed: list[str], run_id: str,
    candidate_digest: str,
) -> dict[str, object]:
    """Classify one code edit for external diagnostics and human REVIEW.

    No candidate-controlled runtime result is interpreted here. The returned
    success is usable only by the retained-candidate preparation path; it is
    not evidence that evals beat baseline or that publication is authorized.
    """
    try:
        valid_run = isinstance(run_id, str) and str(uuid.UUID(run_id)) == run_id
    except (ValueError, AttributeError):
        valid_run = False
    if (
        not isinstance(worktree, str) or not worktree
        or not isinstance(base_commit, str) or _OID.fullmatch(base_commit) is None
        or not isinstance(candidate_digest, str)
        or _DIGEST.fullmatch(candidate_digest) is None
        or not valid_run or not _one_code_path(changed)
    ):
        return {"ok": False, "reason": "candidate is not eligible for supervised REVIEW"}
    return {
        "ok": True,
        "candidate_digest": candidate_digest,
        "required_tier": "review",
        "policy_version": SUPERVISED_POLICY_VERSION,
    }
