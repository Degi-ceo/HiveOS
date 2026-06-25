"""
dataset.py — load EvalItem rows from JSONL or YAML.

JSONL: one EvalItem per line. Required fields: id, input, expected, grader.
YAML: a list of maps with the same fields. YAML loader is optional (requires
PyYAML) — JSONL works without any extra dependency and is the canonical format.

Each row's `extra` field is optional and grader-specific:
  - regex:    {"pattern": "...", "flags": 0}
  - llm_judge:{"rubric": "...", "threshold": 0.7}
  - tool_trace:{"required_tools": ["web_get"], "forbidden_tools": ["bash"]}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from hive.evals.types import EvalItem


class DatasetError(ValueError):
    """Raised on malformed dataset input — distinct from grader errors so the
    runner can exit 2 (data problem) vs 1 (eval failure)."""


def load_jsonl(path: str | Path) -> list[EvalItem]:
    """Parse a .jsonl file. Lines that are blank or start with '#' are skipped.
    Malformed lines raise DatasetError with the offending line number."""
    p = Path(path)
    items: list[EvalItem] = []
    with p.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetError(f"{p}:{lineno}: invalid JSON — {e}") from e
            items.append(_parse_item(obj, source=f"{p}:{lineno}"))
    return items


def load_yaml(path: str | Path) -> list[EvalItem]:
    """Parse a YAML file containing a list of eval items. Requires PyYAML."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise DatasetError(
            "YAML dataset requested but PyYAML is not installed. "
            "Install with `pip install pyyaml` or use a .jsonl dataset."
        ) from e
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise DatasetError(f"{p}: expected a YAML list of items, got {type(data).__name__}")
    return [_parse_item(obj, source=str(p)) for obj in data]


def load(path: str | Path) -> list[EvalItem]:
    """Auto-detect format by suffix and dispatch."""
    p = Path(path)
    if p.suffix.lower() in {".yaml", ".yml"}:
        return load_yaml(p)
    return load_jsonl(p)


def load_many(paths: Iterable[str | Path]) -> list[EvalItem]:
    """Load multiple datasets and concatenate. Duplicate ids are allowed
    (the runner keys results by position, not id) but discouraged."""
    out: list[EvalItem] = []
    for p in paths:
        out.extend(load(p))
    return out


_REQUIRED = ("id", "input", "expected", "grader")


def _parse_item(obj: object, *, source: str) -> EvalItem:
    if not isinstance(obj, dict):
        raise DatasetError(f"{source}: expected object, got {type(obj).__name__}")
    missing = [k for k in _REQUIRED if k not in obj]
    if missing:
        raise DatasetError(f"{source}: missing required keys {missing}")
    extra = obj.get("extra") or {}
    if not isinstance(extra, dict):
        raise DatasetError(f"{source}: 'extra' must be a dict, got {type(extra).__name__}")
    return EvalItem(
        id=str(obj["id"]),
        input=str(obj["input"]),
        expected=str(obj["expected"]),
        grader=str(obj["grader"]),
        extra=dict(extra),
    )
