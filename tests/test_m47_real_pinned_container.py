"""Opt-in real-Docker smoke for the pinned host diagnostic transport.

The ordinary suite skips this test when a digest-pinned image is not supplied.
The dedicated Linux CI job must supply one and must not treat a skip as proof.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from hive.core.candidate_evidence import _CHECKS
from hive.core.pinned_evidence_docker import DockerPinnedEvidenceRunner


@pytest.mark.skipif(
    not os.getenv("HIVE_TEST_PINNED_IMAGE"),
    reason="requires a pre-pulled digest-pinned image and Docker daemon",
)
def test_real_container_compiles_read_only_candidate_without_host_artifacts(tmp_path: Path):
    image = os.environ["HIVE_TEST_PINNED_IMAGE"]
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*@sha256:[0-9a-f]{64}", image)

    repo = tmp_path / "repo"
    checkout = repo / ".worktrees" / ("hive-supervisor-evidence-" + "a" * 32)
    source = checkout / "src" / "hive" / "candidate.py"
    source.parent.mkdir(parents=True)
    bad_source = "def broken(:\n"
    good_source = "answer = 42\n"
    source.write_text(bad_source, encoding="utf-8")

    runner = DockerPinnedEvidenceRunner(image=image, repo_root=repo, timeout_seconds=90)
    bad_result = asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[1][1]))
    assert bad_result[0] != 0
    assert bad_result[1] == ""
    assert source.read_text(encoding="utf-8") == bad_source

    source.write_text(good_source, encoding="utf-8")
    good_result = asyncio.run(runner.run_pinned_evidence(str(checkout), _CHECKS[1][1]))
    assert good_result == (0, "")
    assert source.read_text(encoding="utf-8") == good_source
    assert set(checkout.rglob("*")) == {checkout / "src", checkout / "src" / "hive", source}
