"""Environment filtering for subprocess trust boundaries."""
from __future__ import annotations

import os
from collections.abc import Mapping

PRIVILEGED_CHILD_ENV_KEYS = frozenset({
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "HIVE_APPROVER_KEY",
    "HIVE_AUDIT_INTEGRITY_KEY",
    "HIVE_GITHUB_TOKEN",
    "HIVE_TELEGRAM_APPROVAL_SIGNING_KEY",
})
_PRIVILEGED_CHILD_ENV_KEYS_CASEFOLDED = frozenset(
    key.casefold() for key in PRIVILEGED_CHILD_ENV_KEYS
)


def has_privileged_credentials(env: Mapping[str, str] | None = None) -> bool:
    """Return whether an environment contains a privileged credential name."""
    source = os.environ if env is None else env
    return any(key.casefold() in _PRIVILEGED_CHILD_ENV_KEYS_CASEFOLDED for key in source)


def without_privileged_credentials(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without approval or repository-control credentials."""
    source = os.environ if env is None else env
    return {
        key: value
        for key, value in source.items()
        if key.casefold() not in _PRIVILEGED_CHILD_ENV_KEYS_CASEFOLDED
    }


__all__ = ["has_privileged_credentials", "without_privileged_credentials"]
