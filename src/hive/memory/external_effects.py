"""Conservative contracts for memory projection side effects.

The ledger must make recovery decisions from durable, provider-level facts rather
than exception text.  These contracts intentionally describe only capabilities
that Hive can prove today; an unregistered target is external and non-replayable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProjectionReviewReason(StrEnum):
    """Bounded, non-secret classifications for quarantined projections."""

    EXTERNAL_DELIVERY_INTERRUPTED = "external_delivery_interrupted"
    EXTERNAL_LEASE_EXPIRED = "external_lease_expired"
    EXTERNAL_OUTCOME_UNKNOWN = "external_outcome_unknown"
    EXTERNAL_RECEIPT_MISSING = "external_receipt_missing"
    EXTERNAL_REJECTED = "external_rejected"
    LOCAL_PROJECTION_CONFLICT = "local_projection_conflict"
    LOCAL_PROJECTION_FAILED = "local_projection_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProjectionTargetCapability:
    """Recovery-relevant provider properties, not a permission grant."""

    target: str
    replay_safe: bool
    supports_idempotency_key: bool
    supports_receipt_lookup: bool


_CAPABILITIES = {
    # The managed local vault projection is deterministic and has no remote
    # effect, so an interrupted write can safely be retried.
    "obsidian": ProjectionTargetCapability("obsidian", True, False, False),
    # Mnemosyne currently offers neither a caller-controlled idempotency key nor
    # a receipt lookup that could resolve an unknown remote outcome.
    "mnemosyne": ProjectionTargetCapability("mnemosyne", False, False, False),
}

_DIAGNOSTICS = {
    ProjectionReviewReason.EXTERNAL_DELIVERY_INTERRUPTED: "external delivery interrupted; automatic replay is forbidden",
    ProjectionReviewReason.EXTERNAL_LEASE_EXPIRED: "external delivery lease expired; automatic replay is forbidden",
    ProjectionReviewReason.EXTERNAL_OUTCOME_UNKNOWN: "external delivery outcome is unknown; automatic replay is forbidden",
    ProjectionReviewReason.EXTERNAL_RECEIPT_MISSING: "external delivery receipt is missing; automatic replay is forbidden",
    ProjectionReviewReason.EXTERNAL_REJECTED: "external provider rejected the operation; owner review is required",
    ProjectionReviewReason.LOCAL_PROJECTION_CONFLICT: "managed local projection conflicts with user-authored state",
    ProjectionReviewReason.LOCAL_PROJECTION_FAILED: "deterministic local projection failed",
    ProjectionReviewReason.UNKNOWN: "projection requires owner review",
}


def projection_target_capability(target: str) -> ProjectionTargetCapability:
    """Return a fail-closed contract for a known or future projection target."""
    return _CAPABILITIES.get(
        target,
        ProjectionTargetCapability(target=target, replay_safe=False,
                                   supports_idempotency_key=False, supports_receipt_lookup=False),
    )


def coerce_projection_review_reason(reason: str | ProjectionReviewReason | None) -> ProjectionReviewReason:
    """Reject unbounded provider text instead of persisting it as a reason code."""
    try:
        return ProjectionReviewReason(reason) if reason is not None else ProjectionReviewReason.UNKNOWN
    except ValueError:
        return ProjectionReviewReason.UNKNOWN


def projection_review_diagnostic(reason: str | ProjectionReviewReason | None) -> str:
    """Return a stable safe diagnostic suitable for durable operator state."""
    return _DIAGNOSTICS[coerce_projection_review_reason(reason)]
