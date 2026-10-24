"""Stable host identity and fail-closed startup for managed releases."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import stat
from pathlib import Path

from hive.core.config import HiveConfig
from hive.core.deployment_ledger import DeployLedger
from hive.core.deployment_recovery import DeploymentRecoveryLedger
from hive.core.installed_release import InstalledReleaseSelector

_MAX_MUTABLE_ENTRIES = 50_000


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _check_mutable_tree(path: Path) -> None:
    """Reject existing links in mutable trees before starting an immutable release."""
    if not path.exists():
        return
    if _is_reparse(path):
        raise ValueError("mutable tree contains a reparse point")
    if not path.is_dir():
        return
    pending = [path]
    seen = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                seen += 1
                if seen > _MAX_MUTABLE_ENTRIES or _is_reparse(Path(entry.path)):
                    raise ValueError("mutable tree contains a reparse point or is too large")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _foreign_inflight(db: sqlite3.Connection, host_key: str) -> bool:
    """Do not lose unfinished legacy work when changing the identity root."""
    if db.execute(
        "SELECT 1 FROM deploy_ledger WHERE host_key<>? "
        "AND status IN ('pending', 'verifying') LIMIT 1", (host_key,),
    ).fetchone():
        return True
    return db.execute(
        "SELECT 1 FROM deployment_recovery_ledger WHERE host_key<>? "
        "AND state IN ('prepared', 'staged', 'awaiting_verdict', "
        "'failed', 'uncertain', 'suppressed') LIMIT 1", (host_key,),
    ).fetchone() is not None


def _fenced_host_key(db_path: Path) -> str | None:
    if not db_path.is_file():
        return None
    with sqlite3.connect(db_path, timeout=5.0) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_host_fence'",
        ).fetchone()
        if not exists:
            return None
        row = db.execute(
            "SELECT host_key FROM deployment_host_fence WHERE name='local'",
        ).fetchone()
        if row is None:
            raise ValueError("deployment identity fence is incomplete")
        return str(row[0])


def _install_fence(ledger: DeployLedger, host_key: str) -> None:
    """Atomically reject old work and fence every future old-key write."""
    with ledger._transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS deployment_host_fence "
            "(name TEXT PRIMARY KEY, host_key TEXT NOT NULL)"
        )
        row = db.execute(
            "SELECT host_key FROM deployment_host_fence WHERE name='local'",
        ).fetchone()
        if row is not None and row[0] != host_key:
            raise ValueError("another deployment identity is already fenced")
        if _foreign_inflight(db, host_key):
            raise ValueError("legacy deployment work is still active")
        db.execute(
            "INSERT OR IGNORE INTO deployment_host_fence(name, host_key) VALUES ('local', ?)",
            (host_key,),
        )
        for table in ("deploy_ledger", "deployment_recovery_ledger"):
            for operation in ("INSERT", "UPDATE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS deployment_fence_{table}_{operation.lower()} "
                    f"BEFORE {operation} ON {table} "
                    "WHEN NOT EXISTS(SELECT 1 FROM deployment_host_fence "
                    "WHERE name='local' AND host_key=NEW.host_key) "
                    "BEGIN SELECT RAISE(ABORT, 'deployment identity fenced'); END"
                )


def deployment_host_key(identity_root: Path, *, hostname: str | None = None) -> str:
    """Preserve the legacy host/repository key format for an identity root."""
    identity = f"{hostname or socket.gethostname()}\0{identity_root.resolve()}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def runtime_deployment_host_key(config: HiveConfig, *, loaded_root: Path) -> str:
    """Validate the loaded release and compute its deployment host key."""
    managed = config.installed_release_root
    if managed is None:
        key = deployment_host_key(config.root)
        try:
            fenced = _fenced_host_key(config.state_db)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            raise RuntimeError("deployment identity fence unavailable") from exc
        if fenced is not None and fenced != key:
            raise RuntimeError("legacy deployment identity is fenced")
        return key
    if not config.deploy_verify_enabled:
        raise RuntimeError("HIVE_INSTALLED_RELEASE_ROOT requires HIVE_DEPLOY_VERIFY_ENABLED=true")
    if config.autonomous_selfmod_enabled:
        raise RuntimeError("HIVE_INSTALLED_RELEASE_ROOT requires self-modification to remain disabled")
    if not managed.is_absolute() or managed.is_symlink():
        raise RuntimeError("HIVE_INSTALLED_RELEASE_ROOT must be an absolute, non-symlink directory")
    try:
        root = managed.resolve(strict=True)
        source = config.root.resolve(strict=True)
        loaded = loaded_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("installed release identity unavailable") from exc
    if (managed != root or source != loaded or config.root != source
            or source.parent != root / "releases"):
        raise RuntimeError("running source does not match managed release path")
    for path, directory in (
        (config.data_dir, True), (config.state_db, False),
        (config.mnemosyne_home, True), (config.obsidian_vault, True),
    ):
        resolved = path.resolve()
        if (not path.is_absolute() or path != resolved
                or resolved.is_relative_to(root)
                or (directory and root.is_relative_to(resolved))):
            raise RuntimeError("managed release requires external mutable data paths")
    try:
        for path in (config.data_dir, config.mnemosyne_home, config.obsidian_vault):
            _check_mutable_tree(path)
        if config.state_db.exists() and _is_reparse(config.state_db):
            raise ValueError("state database is a reparse point")
    except (OSError, ValueError) as exc:
        raise RuntimeError("managed release mutable tree is unsafe") from exc
    host_key = deployment_host_key(root)
    try:
        fenced = _fenced_host_key(config.state_db)
        if fenced is not None and fenced != host_key:
            raise ValueError("another deployment identity is already fenced")
        ledger = DeployLedger(config.state_db)
        selector = InstalledReleaseSelector(
            root, ledger, host_key=host_key,
            scope=config.deploy_systemctl_scope,
        )
        selected_sha, selected_path = selector.selected_release()
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("installed release selection unavailable") from exc
    if selected_path != source or selected_sha != source.name:
        raise RuntimeError("running source does not match selected release")
    return host_key


def claim_runtime_deployment_identity(
    config: HiveConfig, *, loaded_root: Path, expected_key: str,
) -> None:
    """Commit the identity switch only after the runtime has fully built."""
    key = runtime_deployment_host_key(config, loaded_root=loaded_root)
    if key != expected_key:
        raise RuntimeError("deployment identity changed during startup")
    if config.installed_release_root is None:
        return
    try:
        ledger = DeployLedger(config.state_db)
        DeploymentRecoveryLedger(config.state_db)
        _install_fence(ledger, key)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("deployment identity handoff unavailable") from exc
