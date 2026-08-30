"""At-least-once, version-aware projection from the Hive ledger to Mnemosyne."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hive.memory.ledger import MemoryLedger, MemoryVersion


class MnemosyneSink(Protocol):
    def remember(self, content: str, **kwargs: Any) -> str: ...

    def invalidate(self, memory_id: str, replacement_id: str | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class MnemosyneProjectionResult:
    operation_id: str
    memory_id: str
    version: int
    state: str


class MnemosyneProjector:
    """Persist canonical versions and invalidate their projected predecessors.

    The binding is persisted immediately after ``remember``. A crash before
    invalidating the predecessor therefore retries the invalidation without
    creating a second active replacement.
    """

    def __init__(self, ledger: MemoryLedger, sink: MnemosyneSink) -> None:
        self._ledger = ledger
        self._sink = sink

    def project_pending(self) -> list[MnemosyneProjectionResult]:
        return [self._project(operation) for operation in self._ledger.pending_projections("mnemosyne")]

    def _project(self, operation: dict) -> MnemosyneProjectionResult:
        memory = self._ledger.get_version(operation["memory_id"], int(operation["version"]))
        try:
            external_id = self._ledger.projection_binding("mnemosyne", memory.memory_id, memory.version)
            if external_id is None:
                external_id = str(self._sink.remember(
                    self._render(memory),
                    source=f"hive-ledger:{memory.source}",
                    importance=0.7,
                    metadata={
                        "hive_memory_id": memory.memory_id,
                        "hive_version": memory.version,
                        "hive_content_hash": memory.content_hash,
                        "hive_stable_key": memory.stable_key,
                    },
                    scope="global",
                ))
                if not external_id:
                    raise RuntimeError("Mnemosyne did not return a memory id")
                self._ledger.record_projection_binding(
                    "mnemosyne", memory.memory_id, memory.version, external_id,
                )
            previous_id = self._ledger.projection_binding(
                "mnemosyne", memory.memory_id, memory.version - 1,
            )
            if previous_id and not self._sink.invalidate(previous_id, replacement_id=external_id):
                raise RuntimeError(f"Mnemosyne rejected invalidation for {previous_id}")
        except Exception:  # noqa: BLE001 - failure remains durable in the outbox
            self._ledger.record_projection_failure(operation["operation_id"])
            return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id, memory.version, "pending")
        self._ledger.mark_projected(operation["operation_id"])
        return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id, memory.version, "applied")

    @staticmethod
    def _render(memory: MemoryVersion) -> str:
        return f"[{memory.kind}] {memory.stable_key}: {memory.content}"
