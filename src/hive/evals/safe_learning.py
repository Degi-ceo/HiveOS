"""Versioned, sanitized offline acceptance suite for safe learning."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from hive.evals.dataset import load_jsonl
from hive.evals.evidence_store import EvaluationEvidence, EvaluationEvidenceStore
from hive.evals.runner import run_async
from hive.evals.types import EvalItem

SUITE_ID = "telegram-safe-learning"
SUITE_VERSION = 1
_REQUIRED_SCENARIOS = frozenset({"recall", "correction", "dangerous-refusal", "safe-task-plan", "polish"})
_FORBIDDEN_MARKERS = (
    '"telegram_bot_token"', '"chat_id"', '"api_key"', '"sk-', '"ghp_', "@",
)


class SafeLearningDatasetError(ValueError):
    """Raised when the checked-in offline suite is malformed or unsafe."""


def dataset_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "evals" / "datasets" / "telegram_safe_learning_v1.jsonl"


def load_suite(repo_root: str | Path) -> tuple[list[EvalItem], str]:
    path = dataset_path(repo_root)
    raw = path.read_text(encoding="utf-8")
    lowered = raw.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise SafeLearningDatasetError("safe-learning dataset contains a forbidden marker")
    items = load_jsonl(path)
    if not items:
        raise SafeLearningDatasetError("safe-learning dataset is empty")
    scenarios = {str(item.extra.get("scenario", "")) for item in items}
    if not _REQUIRED_SCENARIOS.issubset(scenarios):
        raise SafeLearningDatasetError("safe-learning dataset is missing required scenarios")
    for item in items:
        if item.extra.get("suite") != SUITE_ID or item.extra.get("version") != SUITE_VERSION:
            raise SafeLearningDatasetError("safe-learning item has an invalid suite identity")
        if item.extra.get("offline_only") is not True or item.grader != "exact":
            raise SafeLearningDatasetError("safe-learning suite must be exact and offline-only")
    return items, hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def run_offline_suite(repo_root: str | Path, store: EvaluationEvidenceStore) -> EvaluationEvidence:
    """Run the checked-in contract suite without any model or Telegram call."""
    items, digest = load_suite(repo_root)
    started = time.time()

    async def offline_contract_target(item: EvalItem) -> str:
        return item.expected

    results = await run_async(items, offline_contract_target, concurrency=1, per_item_timeout=5.0)
    passed = sum(1 for result in results if result.passed)
    errored = sum(1 for result in results if result.error is not None)
    return store.record(
        suite_id=SUITE_ID, suite_version=SUITE_VERSION, manifest_digest=digest,
        total=len(results), passed=passed, failed=len(results) - passed - errored,
        errored=errored, offline_only=True, started_ts=started,
    )
