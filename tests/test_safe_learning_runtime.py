from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hive.core.config import HiveConfig
from hive.runtime import HiveOS


def test_learning_diagnosis_requires_fresh_safe_learning_evidence(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Keep mutable state isolated in tmp_path while reading the checked-in
    # versioned scenario manifest from this repository.
    cfg = replace(cfg, root=Path(__file__).resolve().parent.parent, learning_loop_enabled=True)
    hive = HiveOS.build(cfg)
    hive.learning_loop.run = AsyncMock(return_value=SimpleNamespace(verdict="reject", worktree_branch=None))

    assert asyncio.run(hive.self_improve_from_symptom("test", use_learning_loop=True)) == []
    hive.learning_loop.run.assert_not_awaited()

    evidence = asyncio.run(hive.run_safe_learning_evaluations())
    assert evidence.all_passed and evidence.offline_only
    assert asyncio.run(hive.self_improve_from_symptom("test", use_learning_loop=True)) == []
    hive.learning_loop.run.assert_awaited_once_with("test")
