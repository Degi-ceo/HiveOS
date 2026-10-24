"""Host-owned release selection metadata for a future verified rollback.

Selection never changes a checkout, Git ref, service unit, or running process.
The service launcher and post-selection health verification are separate,
mandatory boundaries before this can become an automatic code rollback.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from hive.core.child_env import minimal_worker_environment
from hive.core.deployment_ledger import DEGRADED, HEALTHY, DeployLedger, DeployRecord

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class InstalledReleaseSelector:
    """Select only clean, detached installed Git releases under one host root.

    This is a durable catalog, not a launcher or a live rollback mechanism.
    The release root and its SQLite file must be owned by the trusted host.
    """

    def __init__(
        self, root: str | Path, ledger: DeployLedger, *, host_key: str, scope: str,
    ) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or self._is_reparse(candidate):
            raise ValueError("release root must be an absolute managed directory")
        self._root = candidate.resolve(strict=True)
        releases = self._root / "releases"
        if not self._root.is_dir() or not releases.is_dir() or self._is_reparse(releases):
            raise ValueError("release root requires a non-symlink releases directory")
        if scope not in {"system", "user"}:
            raise ValueError("invalid systemctl scope")
        self._host_key = DeployLedger._key(host_key, "host_key")
        self._scope = scope
        self._ledger = ledger
        with self._transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS installed_release_selection (
                    host_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    selected_sha TEXT,
                    selected_generation INTEGER NOT NULL DEFAULT 0,
                    last_good_sha TEXT,
                    last_good_receipt TEXT,
                    PRIMARY KEY (host_key, scope)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS installed_release_attempts (
                    receipt_id TEXT PRIMARY KEY,
                    host_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    selected_generation INTEGER NOT NULL,
                    validated INTEGER NOT NULL DEFAULT 0 CHECK (validated IN (0, 1))
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS installed_release_events (
                    receipt_id TEXT PRIMARY KEY,
                    host_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    from_sha TEXT NOT NULL,
                    to_sha TEXT NOT NULL,
                    selected_at REAL NOT NULL
                )"""
            )
            db.execute(
                "INSERT OR IGNORE INTO installed_release_selection "
                "(host_key, scope) VALUES (?, ?)", (self._host_key, self._scope),
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS installed_release_capture_attempt
                   AFTER INSERT ON deploy_ledger
                   WHEN NEW.status='pending' AND NEW.target='gateway'
                        AND NEW.mode='systemctl' AND NEW.restart_confirmed_at IS NULL
                   BEGIN
                     INSERT INTO installed_release_attempts
                         (receipt_id, host_key, scope, selected_generation)
                     SELECT NEW.id, NEW.host_key, NEW.systemctl_scope, selected_generation
                       FROM installed_release_selection
                      WHERE host_key=NEW.host_key AND scope=NEW.systemctl_scope
                        AND selected_sha=NEW.expected_sha;
                   END"""
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS installed_release_demote_last_good
                   AFTER UPDATE OF status ON deploy_ledger
                   WHEN OLD.status='healthy' AND NEW.status='degraded'
                   BEGIN
                     UPDATE installed_release_selection
                        SET last_good_sha=NULL, last_good_receipt=NULL
                      WHERE last_good_receipt=NEW.id;
                   END"""
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # Share DeployLedger's writer lock: a healthy receipt cannot be
        # demoted between eligibility validation and selection commit.
        with self._ledger._transaction() as db:
            yield db

    @staticmethod
    def _receipt(db: sqlite3.Connection, receipt_id: str | None) -> DeployRecord | None:
        if not isinstance(receipt_id, str):
            return None
        row = db.execute("SELECT * FROM deploy_ledger WHERE id=?", (receipt_id,)).fetchone()
        return DeployLedger._record(row) if row is not None else None

    def _state(self, db: sqlite3.Connection) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM installed_release_selection WHERE host_key=? AND scope=?",
            (self._host_key, self._scope),
        ).fetchone()
        if row is None:
            raise RuntimeError("release selection state is unavailable")
        return row

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @staticmethod
    def _git_environment() -> dict[str, str]:
        env = minimal_worker_environment()
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        env["GIT_NO_LAZY_FETCH"] = "1"
        return env

    def _checked_release(self, sha: str) -> Path:
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            raise ValueError("release SHA must be a full lowercase Git object ID")
        path = self._root / "releases" / sha
        if (self._is_reparse(path) or not path.is_dir()
                or path.resolve(strict=True) != path):
            raise ValueError("release must be an installed non-symlink directory")
        gitfile = path / ".git"
        if self._is_reparse(gitfile) or not gitfile.is_file():
            raise ValueError("release must be an installed Git worktree")
        commands = (
            ("rev-parse", "--verify", "HEAD"),
            ("rev-parse", "--show-toplevel"),
            ("rev-parse", "--abbrev-ref", "HEAD"),
        )
        outputs: list[str] = []
        try:
            for args in commands:
                result = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", *args],
                    cwd=path, env=self._git_environment(),
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=10, check=False,
                )
                if result.returncode != 0 or len(result.stdout) > 4096:
                    raise ValueError("release is not a clean installed release")
                outputs.append(result.stdout.strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("release is not a clean installed release") from exc
        if (outputs[0] != sha or Path(outputs[1]).resolve(strict=True) != path
                or outputs[2:] != ["HEAD"]):
            raise ValueError("release is not a clean installed release")
        self._verify_tree_bytes(path, sha)
        return path

    @staticmethod
    def _verify_tree_bytes(path: Path, sha: str) -> None:
        """Compare real files with HEAD blobs, ignoring mutable index flags."""
        try:
            tree = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "ls-tree", "-r", "-z",
                 "--full-tree", sha],
                cwd=path, env=InstalledReleaseSelector._git_environment(),
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("release is not a clean installed release") from exc
        if (tree.returncode != 0 or not tree.stdout or not tree.stdout.endswith(b"\0")
                or len(tree.stdout) > 8_000_000):
            raise ValueError("release is not a clean installed release")
        total_size = 0
        expected_files: set[str] = set()
        expected_dirs: set[str] = set()
        for entry in tree.stdout.split(b"\0")[:-1]:
            try:
                metadata, raw_name = entry.split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ")
                name = os.fsdecode(raw_name)
                relative = PurePosixPath(name)
                if (
                    kind != b"blob" or mode not in {b"100644", b"100755"}
                    or len(oid) != len(sha) or not name or "\\" in name
                    or relative.is_absolute() or relative.as_posix() != name
                    or any(part.casefold() in {".", "..", ".git"} for part in relative.parts)
                ):
                    raise ValueError
                expected_files.add(name)
                for depth in range(1, len(relative.parts)):
                    expected_dirs.add("/".join(relative.parts[:depth]))
                candidate = path
                for part in relative.parts:
                    candidate = candidate / part
                    if InstalledReleaseSelector._is_reparse(candidate):
                        raise ValueError
                if not candidate.is_file() or not candidate.resolve(strict=True).is_relative_to(path):
                    raise ValueError
                size = candidate.stat().st_size
                total_size += size
                if size > 128_000_000 or total_size > 1_000_000_000:
                    raise ValueError
                digest = hashlib.sha1() if len(oid) == 40 else hashlib.sha256()
                digest.update(f"blob {size}\0".encode("ascii"))
                with candidate.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest().encode("ascii") != oid:
                    raise ValueError
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("release is not a clean installed release") from exc
        seen_files: set[str] = set()
        pending = [(path, "")]
        try:
            while pending:
                directory, prefix = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not prefix and entry.name == ".git":
                            if (InstalledReleaseSelector._is_reparse(Path(entry.path))
                                    or not entry.is_file(follow_symlinks=False)):
                                raise ValueError
                            continue
                        if not entry.name or "\\" in entry.name:
                            raise ValueError
                        relative_name = f"{prefix}/{entry.name}" if prefix else entry.name
                        if InstalledReleaseSelector._is_reparse(Path(entry.path)):
                            raise ValueError
                        if entry.is_dir(follow_symlinks=False):
                            if relative_name not in expected_dirs:
                                raise ValueError
                            pending.append((Path(entry.path), relative_name))
                        elif entry.is_file(follow_symlinks=False):
                            if relative_name not in expected_files:
                                raise ValueError
                            seen_files.add(relative_name)
                        else:
                            raise ValueError
            if seen_files != expected_files:
                raise ValueError
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("release is not a clean installed release") from exc

    def selected_sha(self) -> str | None:
        with self._transaction() as db:
            return self._state(db)["selected_sha"]

    def selected_release(self) -> tuple[str, Path]:
        """Revalidate the exact selected tree for a trusted host launcher."""
        with self._transaction() as db:
            sha = self._state(db)["selected_sha"]
            if not isinstance(sha, str):
                raise ValueError("no installed release is selected")
            return sha, self._checked_release(sha)

    def last_good_sha(self) -> str | None:
        with self._transaction() as db:
            return self._state(db)["last_good_sha"]

    def select_candidate(self, sha: str, *, expected_selected: str | None) -> str:
        """Atomically select a clean installed candidate; do not start it."""
        self._checked_release(sha)
        with self._transaction() as db:
            if self._state(db)["selected_sha"] != expected_selected:
                raise ValueError("selected release changed")
            db.execute(
                "UPDATE installed_release_selection "
                "SET selected_sha=?, selected_generation=selected_generation+1 "
                "WHERE host_key=? AND scope=?",
                (sha, self._host_key, self._scope),
            )
        return sha

    def bind_deploy_receipt(self, receipt_id: str) -> None:
        """Bind one pending process-bound deployment to the selected generation."""
        with self._transaction() as db:
            state = self._state(db)
            record = self._receipt(db, receipt_id)
            if (
                record is None or record.status != "pending"
                or record.host_key != self._host_key or record.systemctl_scope != self._scope
                or record.target != "gateway" or record.mode != "systemctl"
                or record.expected_sha != state["selected_sha"]
                or not isinstance(record.baseline_process_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", record.baseline_process_id)
                or record.started_process_id or record.restart_confirmed_at is not None
            ):
                raise ValueError("pending deployment does not match selected release")
            if not self._bound_to_selection(db, receipt_id, state["selected_generation"],
                                            require_validated=False):
                raise ValueError("receipt was not created in selected generation")
            if state["last_good_sha"] is not None:
                previous = self._receipt(db, state["last_good_receipt"])
                if (
                    not self._eligible_receipt(previous, HEALTHY)
                    or previous.expected_sha != state["last_good_sha"]
                    or record.baseline_sha != previous.expected_sha
                    or record.baseline_process_id != previous.started_process_id
                ):
                    raise ValueError("baseline process does not match last good")
            elif record.baseline_sha:
                raise ValueError("baseline process does not match last good")
            db.execute(
                "UPDATE installed_release_attempts SET validated=1 WHERE receipt_id=?",
                (receipt_id,),
            )

    def _bound_to_selection(self, db: sqlite3.Connection, receipt_id: str,
                            generation: int, *, require_validated: bool = True) -> bool:
        row = db.execute(
            "SELECT host_key, scope, selected_generation, validated FROM installed_release_attempts "
            "WHERE receipt_id=?", (receipt_id,),
        ).fetchone()
        return bool(
            row is not None and row["host_key"] == self._host_key
            and row["scope"] == self._scope and row["selected_generation"] == generation
            and (not require_validated or row["validated"] == 1)
        )

    def _eligible_receipt(self, record: DeployRecord | None, status: str) -> bool:
        return bool(
            record is not None and record.host_key == self._host_key
            and record.systemctl_scope == self._scope
            and record.target == "gateway" and record.mode == "systemctl"
            and record.status == status and record.restart_confirmed_at is not None
            and isinstance(record.baseline_process_id, str)
            and re.fullmatch(r"[0-9a-f]{32}", record.baseline_process_id)
            and isinstance(record.started_process_id, str)
            and re.fullmatch(r"[0-9a-f]{32}", record.started_process_id)
        )

    def record_last_good(self, receipt_id: str) -> str:
        """Retain only a process-bound healthy verdict matching active selection."""
        with self._transaction() as db:
            record = self._receipt(db, receipt_id)
            if not self._eligible_receipt(record, HEALTHY):
                raise ValueError("last good requires a process-bound healthy receipt")
            state = self._state(db)
            if (state["selected_sha"] != record.expected_sha
                    or not self._bound_to_selection(db, receipt_id, state["selected_generation"])):
                raise ValueError("healthy receipt does not match selected release")
            self._checked_release(record.expected_sha)
            db.execute(
                "UPDATE installed_release_selection SET last_good_sha=?, last_good_receipt=? "
                "WHERE host_key=? AND scope=?",
                (record.expected_sha, receipt_id, self._host_key, self._scope),
            )
        return record.expected_sha

    def select_rollback_target(self, receipt_id: str) -> str | None:
        """One-shot select a verified predecessor; never restart or claim health."""
        with self._transaction() as db:
            record = self._receipt(db, receipt_id)
            if not self._eligible_receipt(record, DEGRADED):
                raise ValueError("rollback selection requires a process-bound degraded receipt")
            state = self._state(db)
            if db.execute(
                "SELECT 1 FROM installed_release_events WHERE receipt_id=?", (receipt_id,),
            ).fetchone() is not None:
                return None
            target = state["last_good_sha"]
            last_good_receipt = self._receipt(db, state["last_good_receipt"])
            if (
                not target or target != record.baseline_sha
                or state["selected_sha"] != record.expected_sha
                or target == record.expected_sha
                or not self._bound_to_selection(db, receipt_id, state["selected_generation"])
                or not self._eligible_receipt(last_good_receipt, HEALTHY)
                or last_good_receipt.expected_sha != target
                or record.baseline_process_id != last_good_receipt.started_process_id
            ):
                raise ValueError("no matching last-good release or selection generation")
            self._checked_release(target)
            db.execute(
                "UPDATE installed_release_selection "
                "SET selected_sha=?, selected_generation=selected_generation+1 "
                "WHERE host_key=? AND scope=?",
                (target, self._host_key, self._scope),
            )
            db.execute(
                "INSERT INTO installed_release_events "
                "(receipt_id,host_key,scope,run_id,from_sha,to_sha,selected_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (receipt_id, self._host_key, self._scope, record.run_id,
                 record.expected_sha, target, time.time()),
            )
            return target

    def rollback_events(self) -> tuple[tuple[str, str, str, str], ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT run_id,receipt_id,from_sha,to_sha FROM installed_release_events "
                "WHERE host_key=? AND scope=? ORDER BY selected_at, rowid",
                (self._host_key, self._scope),
            ).fetchall()
            return tuple(tuple(row) for row in rows)
