"""Shared pytest fixtures for the HiveOS test suite."""
from __future__ import annotations

import pytest

from hive.core.approval import gate as _approval_gate


@pytest.fixture(autouse=True)
def _clear_approval_gate():
    """Reset the module-level approval-gate singleton before every test.

    Without this, REVIEW-tier self-improve tests that call gate.request()
    without resolving the item leave stale entries that pollute later tests
    which check pending approvals."""
    _approval_gate._pending.clear()
    yield
    _approval_gate._pending.clear()
