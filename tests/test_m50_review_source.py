"""A code review suggestion must stay within the supervised candidate path."""

from __future__ import annotations

import asyncio

import pytest

from hive.core.pr_review import ReviewSuggestion
from hive.runtime import _review_line_apply


def _suggestion(path: str) -> ReviewSuggestion:
    return ReviewSuggestion(
        thread_id="thread-1", comment_id="comment-1", reviewer_id=1234,
        path=path, line=2, replacement="    return value + 1",
        head_sha="a" * 40, signal_digest="b" * 64,
    )


def test_one_indented_source_line_needs_explicit_supervised_route(tmp_path):
    root = tmp_path / "candidate"
    source = root / "src" / "hive" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("def get(value):\n    return value\n", encoding="utf-8")
    suggestion = _suggestion("src/hive/example.py")

    with pytest.raises(ValueError, match="review target"):
        asyncio.run(_review_line_apply(suggestion, frozenset())(str(root)))
    assert source.read_text(encoding="utf-8") == "def get(value):\n    return value\n"

    assert asyncio.run(_review_line_apply(
        suggestion, frozenset(), allow_code=True,
    )(str(root))) == ["src/hive/example.py"]
    assert source.read_text(encoding="utf-8") == "def get(value):\n    return value + 1\n"


def test_supervised_review_still_rejects_evaluation_controls(tmp_path):
    root = tmp_path / "candidate"
    source = root / "tests" / "conftest.py"
    source.parent.mkdir(parents=True)
    source.write_text("def get(value):\n    return value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="review target"):
        asyncio.run(_review_line_apply(
            _suggestion("tests/conftest.py"), frozenset(), allow_code=True,
        )(str(root)))
    assert source.read_text(encoding="utf-8") == "def get(value):\n    return value\n"
