"""At-least-once, version-aware projection from the Hive ledger to Mnemosyne."""
from __future__ import annotations

import uuid
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

    def __init__(self, ledger: MemoryLedger, sink: MnemosyneSink, *, worker_id: str | None = None,
                 lease_seconds: float = 300.0) -> None:
        self._ledger = ledger
        self._sink = sink
        self._worker_id = worker_id or f"mnemosyne-{uuid.uuid4().hex}"
        self._lease_seconds = lease_seconds

    def project_pending(self) -> list[MnemosyneProjectionResult]:
        return [self._project(operation) for operation in self._ledger.claim_pending_projections(
            "mnemosyne", worker_id=self._worker_id, lease_seconds=self._lease_seconds,
        )]

    def _project(self, operation: dict) -> MnemosyneProjectionResult:
        memory = self._ledger.get_version(operation["memory_id"], int(operation["version"]))
        external_id = self._ledger.projection_binding("mnemosyne", memory.memory_id, memory.version)
        if external_id is None:
            try:
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
            except Exception as exc:  # response loss can hide a successful external write
                self._ledger.quarantine_projection(
                    operation["operation_id"], worker_id=self._worker_id,
                    detail=f"unknown external outcome while calling Mnemosyne remember: {exc}",
                )
                return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id,
                                                 memory.version, "requires_review")
            if not external_id:
                self._ledger.quarantine_projection(
                    operation["operation_id"], worker_id=self._worker_id,
                    detail="unknown external outcome while calling Mnemosyne remember: no memory id returned",
                )
                return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id,
                                                 memory.version, "requires_review")
            self._ledger.record_projection_binding(
                "mnemosyne", memory.memory_id, memory.version, external_id,
            )
        previous_id = self._ledger.projection_binding(
            "mnemosyne", memory.memory_id, memory.version - 1,
        )
        if previous_id:
            try:
                invalidated = self._sink.invalidate(previous_id, replacement_id=external_id)
            except Exception as exc:  # no receipt: do not repeat an external call automatically
                self._ledger.quarantine_projection(
                    operation["operation_id"], worker_id=self._worker_id,
                    detail=f"unknown external outcome while invalidating Mnemosyne memory: {exc}",
                )
                return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id,
                                                 memory.version, "requires_review")
            if not invalidated:
                self._ledger.quarantine_projection(
                    operation["operation_id"], worker_id=self._worker_id,
                    detail=f"Mnemosyne rejected invalidation for {previous_id}",
                )
                return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id,
                                                 memory.version, "requires_review")
        self._ledger.mark_projected(operation["operation_id"], worker_id=self._worker_id)
        return MnemosyneProjectionResult(operation["operation_id"], memory.memory_id,
                                         memory.version, "applied")

    @staticmethod
    def _render(memory: MemoryVersion) -> str:
        return f"[{memory.kind}] {memory.stable_key}: {memory.content}"
