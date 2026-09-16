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


_WORKER_ENV_KEYS = frozenset({
    # A local Python child needs only operating-system launch variables.  This
    # is deliberately an allowlist: adding a new Hive setting or credential to
    # the parent environment cannot accidentally grant it to a worker.
    "COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
})
_WORKER_ENV_KEYS_CASEFOLDED = frozenset(key.casefold() for key in _WORKER_ENV_KEYS)


def minimal_worker_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the credential-free launch environment for a local worker.

    This intentionally excludes every ``HIVE_*`` setting and arbitrary API
    key/password variable rather than trying to recognise their names.  The
    supervisor, not the worker, owns model credentials and tool capabilities.
    """
    source = os.environ if env is None else env
    return {
        key: value for key, value in source.items()
        if key.casefold() in _WORKER_ENV_KEYS_CASEFOLDED
    }


__all__ = [
    "has_privileged_credentials", "minimal_worker_environment",
    "without_privileged_credentials",
]
