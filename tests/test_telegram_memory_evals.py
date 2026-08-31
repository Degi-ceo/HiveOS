"""Offline contract checks for sanitized Telegram memory-pilot evaluations."""
from __future__ import annotations

from pathlib import Path

from hive.evals.dataset import load_jsonl


DATASET = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "telegram_memory_pilot.jsonl"


def test_telegram_memory_pilot_dataset_is_sanitized_and_offline_only():
    items = load_jsonl(DATASET)

    assert len(items) == 6
    assert {item.extra["scenario"] for item in items} == {
        "correction", "freshness", "provenance", "external-boundary", "safety-gate", "interaction-quality",
    }
    assert all(item.extra.get("offline_only") is True for item in items)
    text = DATASET.read_text(encoding="utf-8").lower()
    assert "token" not in "\n".join(item.input for item in items).lower()
    assert "chat_id" not in text
    assert "@" not in text


def test_telegram_memory_pilot_dataset_passes_deterministic_offline_runner():
    items = load_jsonl(DATASET)

    assert all(item.grader == "exact" and item.expected for item in items)
