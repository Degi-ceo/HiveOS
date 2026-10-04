"""Runtime wiring and safety regressions for bounded candidate repairs (#133)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from hive.core.config import HiveConfig
from hive.core.self_mod import CandidateFailure
from hive.core.spec_search import Edit, EditOp
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _RepairRouter:
    def __init__(self, response: dict[str, str] | None = None) -> None:
        self.response = response or {"old_text": "value = 0", "new_text": "value = 1"}
        self.prompts: list[str] = []

    async def complete(self, messages, **_kwargs):
        self.prompts.append(messages[0].content)
        return CompletionResult(text=json.dumps(self.response), model="test-model")

    async def aclose(self) -> None:
        pass


def _edit(*, summary: str = "Fix the candidate test") -> Edit:
    async def initial_apply(_worktree: str) -> list[str]:
        return ["tests/test_candidate.py"]

    return Edit(
        op=EditOp.ADD_TEST,
        summary=summary,
        apply=initial_apply,
        target_files=["tests/test_candidate.py"],
    )


def _failure(*, test_log: str, staged_diff: str) -> CandidateFailure:
    return CandidateFailure(
        attempt=1,
        test_log=test_log,
        fingerprint="failure-fingerprint",
        staged_diff=staged_diff,
        run_id="repair-run-133",
    )


@pytest.fixture
def repair_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_SELFMOD_MAX_REPAIR_ATTEMPTS", raising=False)
    credential = "m20-private-repair-credential"
    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False,
        host="127.0.0.1",
        production_mode=False,
        secret=credential,
    )
    router = _RepairRouter()
    hive = HiveOS.build(config, router=router)
    try:
        yield hive, router, credential
    finally:
        asyncio.run(hive.aclose())


def test_repair_prompt_contains_redacted_bounded_candidate_evidence(
    repair_runtime, tmp_path,
):
    hive, router, credential = repair_runtime
    router.response = {"old_text": "value = 0", "new_text": "value = 1"}
    repair = hive.improver._repair_factory(_edit(summary=f"Fix test; token={credential}"))
    assert repair is not None

    failure = _failure(
        test_log=f"FAILED tests/test_candidate.py::test_value token={credential}\n" + "L" * 4000,
        staged_diff=f"--- a/tests/test_candidate.py\n+value = 2 token={credential}\n" + "D" * 4000,
    )
    apply_repair = asyncio.run(repair(failure))

    assert apply_repair is not None
    assert len(router.prompts) == 1
    prompt = router.prompts[0]
    assert "FAILED tests/test_candidate.py::test_value" in prompt
    assert "+value = 2" in prompt
    assert "Fix test" in prompt
    assert credential not in prompt
    assert "***REDACTED***" in prompt
    assert "<untrusted-content" in prompt
    assert len(prompt) < 4000

    candidate = tmp_path / "candidate"
    target = candidate / "tests" / "test_candidate.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 0\n", encoding="utf-8")
    assert asyncio.run(apply_repair(str(candidate))) == ["tests/test_candidate.py"]
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize(
    "dangerous_code",
    ["exec('unsafe action')", "subprocess.run(['unsafe', 'action'])"],
)
def test_repair_rejects_payload_that_escalates_auto_safety_tier(
    repair_runtime, dangerous_code,
):
    hive, router, _credential = repair_runtime
    router.response = {"old_text": "value = 0", "new_text": dangerous_code}
    repair = hive.improver._repair_factory(_edit())
    assert repair is not None

    result = asyncio.run(repair(_failure(
        test_log="FAILED tests/test_candidate.py::test_value",
        staged_diff="+value = 0",
    )))

    assert len(router.prompts) == 1
    assert result is None


def test_repair_checks_final_file_after_fragment_replacement(repair_runtime, tmp_path):
    hive, router, _credential = repair_runtime
    router.response = {"old_text": "passthrough", "new_text": "run"}
    repair = hive.improver._repair_factory(_edit())
    assert repair is not None
    apply_repair = asyncio.run(repair(_failure(
        test_log="FAILED tests/test_candidate.py::test_value",
        staged_diff="+subprocess.passthrough([])",
    )))
    assert apply_repair is not None

    candidate = tmp_path / "candidate-cross-boundary"
    target = candidate / "tests" / "test_candidate.py"
    target.parent.mkdir(parents=True)
    original = "import subprocess\nsubprocess.passthrough([])\n"
    target.write_text(original, encoding="utf-8")
    assert asyncio.run(apply_repair(str(candidate))) == []
    assert target.read_text(encoding="utf-8") == original


def test_repair_rejects_dangerous_call_split_across_python_lines(repair_runtime, tmp_path):
    hive, router, _credential = repair_runtime
    router.response = {"old_text": "placeholder", "new_text": "run"}
    repair = hive.improver._repair_factory(_edit())
    assert repair is not None
    apply_repair = asyncio.run(repair(_failure(
        test_log="FAILED tests/test_candidate.py::test_value",
        staged_diff="+subprocess.\\\\nplaceholder([])",
    )))
    assert apply_repair is not None

    candidate = tmp_path / "candidate-continued-call"
    target = candidate / "tests" / "test_candidate.py"
    target.parent.mkdir(parents=True)
    original = "import subprocess\nsubprocess.\\\nplaceholder([])\n"
    target.write_text(original, encoding="utf-8")
    assert asyncio.run(apply_repair(str(candidate))) == []
    assert target.read_text(encoding="utf-8") == original


def test_build_passes_config_only_secret_to_modifier(repair_runtime):
    hive, _router, credential = repair_runtime
    assert credential in hive.self_modifier._repair_secret_values


def test_repair_attempt_default_is_two_and_env_override_is_honored(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_SELFMOD_MAX_REPAIR_ATTEMPTS", raising=False)
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert config.selfmod_max_repair_attempts == 2

    monkeypatch.setenv("HIVE_SELFMOD_MAX_REPAIR_ATTEMPTS", "1")
    overridden = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert overridden.selfmod_max_repair_attempts == 1


@pytest.mark.parametrize(("configured", "expected"), [(None, 2), ("1", 1)])
def test_real_build_wires_configured_repair_attempt_limit(
    tmp_path, monkeypatch, configured, expected,
):
    if configured is None:
        monkeypatch.delenv("HIVE_SELFMOD_MAX_REPAIR_ATTEMPTS", raising=False)
    else:
        monkeypatch.setenv("HIVE_SELFMOD_MAX_REPAIR_ATTEMPTS", configured)
    config = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        autonomy_enabled=False,
        host="127.0.0.1",
        production_mode=False,
        secret="change_me",
    )
    router = _RepairRouter()
    hive = HiveOS.build(config, router=router)
    try:
        assert hive.improver._max_repair_attempts == expected
        assert hive.improver._repair_factory(_edit()) is not None
    finally:
        asyncio.run(hive.aclose())
