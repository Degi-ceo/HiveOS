"""Shared pytest fixtures for the HiveOS test suite."""
from __future__ import annotations

import pytest

from hive.core.approval import gate as _approval_gate
import hive.core.config as _config_mod


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset module-level singletons before and after every test to prevent
    state leakage between tests:

    - approval gate _pending: REVIEW-tier self-improve tests enqueue
      approvals without resolving them; stale entries pollute later tests
      that check pending approvals.
    - _CONFIG: HiveOS.build() calls set_config(cfg) which mutates the
      module-level global; without a reset a test that calls get_config()
      without building first may see another test's config.
    """
    saved_config = _config_mod._CONFIG
    _approval_gate._pending.clear()
    yield
    _approval_gate._pending.clear()
    _config_mod._CONFIG = saved_config
