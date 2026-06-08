"""
credentials.py — local credential store with strict perms + env injection.

Ported from OpenJarvis core/credentials.py (Docs/references/OPENJARVIS_REFERENCE.md
§3.1). Stores secrets in a 0o600 JSON file under the data dir; `inject()` loads
them into os.environ before provider calls. Secrets never live in code or config.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from hiveos.core import config

log = logging.getLogger("hiveos.credentials")

_PATH = Path(config.DATA_DIR) / "credentials.json"


def _load() -> dict[str, str]:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("credentials read failed: %s", exc)
        return {}


def save(key: str, value: str) -> None:
    data = _load()
    data[key] = value
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(_PATH, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass


def get(key: str, default: str | None = None) -> str | None:
    return _load().get(key, os.getenv(key, default))


def inject() -> int:
    """Load stored credentials into os.environ (does not overwrite existing)."""
    n = 0
    for k, v in _load().items():
        if k not in os.environ:
            os.environ[k] = v
            n += 1
    return n
