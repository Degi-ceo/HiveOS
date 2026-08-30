"""Safe, derived Obsidian projection for canonical Hive memories.

The projector owns only its configured root. It never scans, renames, or
overwrites user-authored vault notes outside that root.
"""
from __future__ import annotations

import hashlib
import json
import os
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

    def __init__(self, ledger: MemoryLedger, root: str | Path) -> None:
        self._ledger = ledger
        self._root = Path(root)

    def project_pending(self) -> list[ProjectionResult]:
        return [self._project(operation) for operation in self._ledger.pending_projections("obsidian")]

    def _project(self, operation: dict) -> ProjectionResult:
        memory = self._ledger.get_version(operation["memory_id"], int(operation["version"]))
        path = self._note_path(memory)
        rendered = self._render(memory)
        manifest = self._manifest_path(memory)
        if self._has_user_conflict(path, manifest):
            self._ledger.record_projection_failure(operation["operation_id"])
            return ProjectionResult(operation["operation_id"], path, "conflict")
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
        self._ledger.mark_projected(operation["operation_id"])
        return ProjectionResult(operation["operation_id"], path, "applied")

    def _note_path(self, memory: MemoryVersion) -> Path:
        folder = _FOLDERS.get(memory.kind.lower(), "00 Inbox")
        return self._root / folder / f"{memory.memory_id}.md"

    def _manifest_path(self, memory: MemoryVersion) -> Path:
        return self._root / "_System" / "manifests" / f"{memory.memory_id}.json"

    def _has_user_conflict(self, path: Path, manifest_path: Path) -> bool:
        if not path.exists():
            return False
        if not manifest_path.exists():
            return True
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return manifest.get("rendered_hash") != self._digest(path.read_text(encoding="utf-8"))

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
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
