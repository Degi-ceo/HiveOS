"""Safe, derived Obsidian projection for canonical Hive memories.

The projector owns only its configured root. It never scans, renames, or
overwrites user-authored vault notes outside that root.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from hive.memory.ledger import MemoryLedger, MemoryVersion

_FOLDERS = {
    "decision": "30 Decisions",
    "lesson": "40 Lessons",
    "knowledge": "50 Knowledge",
    "fact": "50 Knowledge",
    "procedure": "60 Procedures",
    "skill": "60 Procedures",
    "incident": "70 Incidents",
    "session": "80 Sessions",
}


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    operation_id: str
    path: Path
    state: str


class ObsidianShadowProjector:
    """Project ledger versions into a managed subtree using atomic replacement."""

    def __init__(self, ledger: MemoryLedger, root: str | Path, *, worker_id: str | None = None,
                 lease_seconds: float = 300.0) -> None:
        self._ledger = ledger
        self._root = Path(root)
        self._worker_id = worker_id or f"obsidian-{uuid.uuid4().hex}"
        self._lease_seconds = lease_seconds

    def project_pending(self) -> list[ProjectionResult]:
        results: list[ProjectionResult] = []
        for operation in self._ledger.claim_pending_projections(
            "obsidian", worker_id=self._worker_id, lease_seconds=self._lease_seconds,
        ):
            try:
                results.append(self._project(operation))
            except Exception:
                memory = self._ledger.get_version(operation["memory_id"], int(operation["version"]))
                results.append(ProjectionResult(operation["operation_id"], self._note_path(memory), "pending"))
        return results

    def _project(self, operation: dict) -> ProjectionResult:
        memory = self._ledger.get_version(operation["memory_id"], int(operation["version"]))
        path = self._note_path(memory)
        history_path = self._history_path(memory)
        rendered = self._render(memory)
        manifest = self._manifest_path(memory)
        if self._has_history_conflict(history_path, rendered):
            self._ledger.quarantine_projection(
                operation["operation_id"], worker_id=self._worker_id,
                detail="manual edit of immutable managed history requires review",
            )
            return ProjectionResult(operation["operation_id"], history_path, "conflict")
        if self._has_user_conflict(path, manifest, rendered):
            self._ledger.quarantine_projection(
                operation["operation_id"], worker_id=self._worker_id,
                detail="manual edit or invalid managed-note manifest requires review",
            )
            return ProjectionResult(operation["operation_id"], path, "conflict")
        try:
            self._atomic_write(path, rendered)
            self._atomic_write(
                manifest,
                json.dumps(
                    {
                        "memory_id": memory.memory_id,
                        "version": memory.version,
                        "note": str(path.relative_to(self._root)),
                        "rendered_hash": self._digest(rendered),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            self._atomic_write(history_path, rendered)
        except Exception:
            self._ledger.record_projection_failure(
                operation["operation_id"], worker_id=self._worker_id,
                detail="local Obsidian projection failed; deterministic retry is allowed",
            )
            raise
        self._ledger.mark_projected(operation["operation_id"], worker_id=self._worker_id)
        return ProjectionResult(operation["operation_id"], path, "applied")

    def _note_path(self, memory: MemoryVersion) -> Path:
        folder = _FOLDERS.get(memory.kind.lower(), "00 Inbox")
        return self._root / folder / f"{memory.memory_id}.md"

    def _manifest_path(self, memory: MemoryVersion) -> Path:
        return self._root / "_System" / "manifests" / f"{memory.memory_id}.json"

    def _history_path(self, memory: MemoryVersion) -> Path:
        return self._root / "_System" / "history" / memory.memory_id / f"v{memory.version}.md"

    @staticmethod
    def _has_history_conflict(path: Path, expected_rendered: str) -> bool:
        """History versions are immutable: only an identical retry is safe."""
        if not path.exists():
            return False
        try:
            return path.read_text(encoding="utf-8") != expected_rendered
        except OSError:
            return True

    @staticmethod
    def _has_user_conflict(path: Path, manifest_path: Path, expected_rendered: str) -> bool:
        """Allow deterministic recovery and known managed upgrades, never manual edits.

        A matching expected note proves an interrupted write is safe to finish even if
        its manifest is missing or stale. For an older managed version, the prior
        manifest must attest to the existing note hash before it can be replaced.
        """
        if not path.exists():
            return False
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            return True
        if existing == expected_rendered:
            return False
        if not manifest_path.exists():
            return True
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return manifest.get("rendered_hash") != ObsidianShadowProjector._digest(existing)

    @staticmethod
    def _render(memory: MemoryVersion) -> str:
        scalar = json.dumps
        return (
            "---\n"
            f"hive_memory_id: {scalar(memory.memory_id)}\n"
            f"hive_version: {memory.version}\n"
            f"hive_content_hash: {scalar(memory.content_hash)}\n"
            f"kind: {scalar(memory.kind)}\n"
            f"stable_key: {scalar(memory.stable_key)}\n"
            f"source: {scalar(memory.source)}\n"
            f"provenance_kind: {scalar(memory.provenance_kind)}\n"
            f"confidence: {memory.confidence}\n"
            f"observed_ts: {scalar(memory.observed_ts)}\n"
            f"fresh_until_ts: {scalar(memory.fresh_until_ts)}\n"
            f"veracity: {scalar(memory.veracity)}\n"
            f"correction_of_version: {scalar(memory.correction_of_version)}\n"
            f"correction_reason: {scalar(memory.correction_reason)}\n"
            "managed_by: \"HiveOS canonical ledger\"\n"
            "---\n\n"
            f"# {memory.kind}: {memory.stable_key}\n\n"
            f"{memory.content.rstrip()}\n"
        )

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
