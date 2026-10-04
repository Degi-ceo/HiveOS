"""Regression coverage for bounded, provenance-tagged diagnoser source context."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from hive.core.config import HiveConfig
from hive.core.redact import known_secret_values
from hive.core.types import ContentEnvelope, ContentTrust
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _Router:
    def __init__(self, response: list[dict] | Exception | None = None) -> None:
        self.response = response if response is not None else []
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, messages, *, system="", **_kwargs):
        self.prompts.append(messages[0].content)
        self.systems.append(system)
        if isinstance(self.response, Exception):
            raise self.response
        return CompletionResult(text=json.dumps(self.response), model="test")

    async def aclose(self):
        pass


def _seed(root: Path, relative: str = "src/hive/calculator.py") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def calculate_total(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    return target


def _hive(root: Path, router: _Router) -> HiveOS:
    return HiveOS.build(HiveConfig.from_env(root=root, load_dotenv=False), router=router)


def _diagnose(hive: HiveOS, monkeypatch, symptom: str | ContentEnvelope):
    from hive.core import spec_search

    captured = []

    async def capture(diagnoser, context, _improver):
        captured.extend(await diagnoser(context))
        return []

    monkeypatch.setattr(spec_search, "diagnose_and_run", capture)
    asyncio.run(hive.self_improve_from_symptom(symptom, _already_enriched=True))
    return captured


def _edit(**overrides):
    item = {
        "op": "edit_file",
        "path": "src/hive/calculator.py",
        "start_line": 2,
        "end_line": 2,
        "old_text": "return value + 1",
        "new_text": "return value + 2",
        "summary": "fix calculation",
        "rationale": "correct the result",
    }
    item.update(overrides)
    return item


def test_seeded_code_reaches_prompt_as_untrusted_without_changing_symptom_trust(
    tmp_path, monkeypatch,
):
    _seed(tmp_path)
    router = _Router([_edit()])
    hive = _hive(tmp_path, router)
    symptom = ContentEnvelope.untrusted("calculate_total is wrong", source="web:report")

    edits = _diagnose(hive, monkeypatch, symptom)

    prompt = router.prompts[0]
    assert "return value + 1" in prompt
    assert "Use EDIT_FILE with start_line/end_line" in prompt
    assert "do not emit PATCH_CODE for a new logic fix" in router.systems[0]
    assert '<untrusted-content source="repo:src/hive/calculator.py">' in prompt
    assert '<untrusted-content source="web:report">' in prompt
    assert "Source files (ranked by relevance)" not in prompt
    assert edits[0].origin_trust is ContentTrust.UNTRUSTED
    assert edits[0].origin_source == "web:report"


def test_code_context_is_byte_bounded_and_deterministic(tmp_path, monkeypatch):
    for number in range(30):
        target = tmp_path / "src" / "hive" / f"needle_{number:02}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"def needle_{number:02}():\n    return 'é' * 100 + '{number}'\n",
            encoding="utf-8",
        )
    router = _Router()
    hive = _hive(tmp_path, router)

    _diagnose(hive, monkeypatch, "needle is broken")
    _diagnose(hive, monkeypatch, "needle is broken")

    assert router.prompts[0] == router.prompts[1]
    source_block = router.prompts[0].split("Retrieved code regions:\n", 1)[1].split(
        "\n\nSymptom:", 1,
    )[0]
    assert len(source_block.encode("utf-8")) <= 4096
    assert "needle_00.py" in source_block
    assert "needle_29.py" not in source_block


def test_edit_file_maps_to_patch_code_and_applies_only_the_seen_line(
    tmp_path, monkeypatch,
):
    _seed(tmp_path)
    router = _Router([_edit()])
    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    from hive.core.spec_search import EditOp

    assert len(edits) == 1
    assert edits[0].op is EditOp.PATCH_CODE
    worktree = tmp_path / "candidate"
    _seed(worktree)
    assert asyncio.run(edits[0].apply(str(worktree))) == ["src/hive/calculator.py"]
    assert "return value + 2" in (worktree / "src/hive/calculator.py").read_text(
        encoding="utf-8",
    )


@pytest.mark.parametrize("change", [
    {"start_line": 3, "end_line": 3},
    {"start_line": True},
    {"end_line": 1},
    {"old_text": "not shown to the model"},
    {"path": "../outside.py"},
    {"op": "patch_code", "start_line": 999, "end_line": 999},
])
def test_edit_file_rejects_invalid_or_unseen_ranges(tmp_path, monkeypatch, change):
    _seed(tmp_path)
    router = _Router([_edit(**change)])

    assert _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total") == []


def test_seen_line_is_revalidated_in_candidate_worktree(tmp_path, monkeypatch):
    _seed(tmp_path)
    edits = _diagnose(_hive(tmp_path, _Router([_edit()])), monkeypatch, "calculate_total")
    candidate = tmp_path / "candidate"
    target = _seed(candidate)
    target.write_text("def calculate_total(value):\n    return value + 99\n", encoding="utf-8")

    assert asyncio.run(edits[0].apply(str(candidate))) == []
    assert "return value + 99" in target.read_text(encoding="utf-8")


def test_legacy_patch_code_without_range_remains_accepted(tmp_path, monkeypatch):
    router = _Router([_edit(
        op="patch_code", path="docs/NOTES.md", old_text="old", new_text="new",
        start_line=None, end_line=None,
    )])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "notes need updating")

    assert len(edits) == 1
    assert edits[0].target_files == ["docs/NOTES.md"]


def test_no_hit_and_malicious_index_path_never_enter_prompt(tmp_path, monkeypatch):
    router = _Router()
    hive = _hive(tmp_path, router)
    _diagnose(hive, monkeypatch, "nonexistent_unique_symbol")
    assert "No matching source regions were retrieved." in router.prompts[0]

    from hive.tools.code_search import CodeIndex

    monkeypatch.setattr(CodeIndex, "search_text", lambda *_a, **_k: [{
        "file": "../private.py", "line": 1, "context": "1: NEVER_SHOW_THIS",
    }])
    monkeypatch.setattr(CodeIndex, "search_symbol", lambda *_a, **_k: [])
    _diagnose(hive, monkeypatch, "NEVER_SHOW_THIS")
    source_block = router.prompts[1].split("Retrieved code regions:\n", 1)[1].split(
        "\n\nSymptom:", 1,
    )[0]
    assert "NEVER_SHOW_THIS" not in source_block


def test_critical_diagnosis_forces_index_refresh(tmp_path, monkeypatch):
    _seed(tmp_path)
    from hive.tools.code_search import CodeIndex

    refresh = CodeIndex.refresh
    flags = []

    def observe(self, *, force=False):
        flags.append(force)
        return refresh(self, force=force)

    monkeypatch.setattr(CodeIndex, "refresh", observe)
    _diagnose(_hive(tmp_path, _Router()), monkeypatch, "calculate_total")
    assert True in flags


def test_retrieved_code_is_not_logged_on_router_failure(tmp_path, monkeypatch, caplog):
    target = _seed(tmp_path)
    marker = "PRIVATE_CODE_MARKER"
    target.write_text(f"def calculate_total():\n    return '{marker}'\n", encoding="utf-8")
    router = _Router(RuntimeError(marker))

    assert _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total") == []
    assert marker not in caplog.text


def test_configured_secret_region_is_omitted_before_model_prompt(
    tmp_path, monkeypatch,
):
    secret = "configured-source-secret-482719"
    target = _seed(tmp_path)
    target.write_text(f"def calculate_total():\n    return '{secret}'\n", encoding="utf-8")
    router = _Router()
    hive = _hive(tmp_path, router)
    monkeypatch.setenv("HIVE_SECRET", secret)

    _diagnose(hive, monkeypatch, "calculate_total")

    assert secret not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


def test_malformed_operation_does_not_abort_other_edits(tmp_path, monkeypatch):
    _seed(tmp_path)
    router = _Router([_edit(op={"not": "a string"}), _edit()])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    assert len(edits) == 1


def test_retrieved_code_downgrades_trusted_add_test_and_rejects_code_target(
    tmp_path, monkeypatch,
):
    _seed(tmp_path)
    allowed = _edit(
        op="add_test", path="tests/test_calculator.py", start_line=None,
        end_line=None, old_text="", new_text="def test_total(): pass\n",
    )
    rejected = {**allowed, "path": "src/hive/runtime.py"}
    router = _Router([rejected, allowed])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    from hive.core.spec_search import RiskTier, tiered

    assert len(edits) == 1
    assert edits[0].target_files == ["tests/test_calculator.py"]
    assert edits[0].origin_trust is ContentTrust.UNTRUSTED
    assert tiered(edits)[0].risk_tier is RiskTier.REVIEW


def test_no_retrieved_code_preserves_trusted_symptom_origin(tmp_path, monkeypatch):
    router = _Router([_edit(
        op="add_test", path="tests/test_new.py", start_line=None,
        end_line=None, old_text="", new_text="def test_new(): pass\n",
    )])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "no matching source")

    assert len(edits) == 1
    assert edits[0].origin_trust is ContentTrust.TRUSTED


def test_failed_proposal_hint_downgrades_auto_even_without_code(tmp_path, monkeypatch):
    router = _Router([_edit(
        op="add_test", path="tests/test_new.py", start_line=None,
        end_line=None, old_text="", new_text="def test_new(): pass\n",
    )])
    hive = _hive(tmp_path, router)
    monkeypatch.setattr(
        type(hive.self_modifier), "failed_proposals",
        lambda _self, *, limit: [{"title": "prior failure", "stage": "test"}],
    )

    edits = _diagnose(hive, monkeypatch, "no matching source")

    from hive.core.spec_search import RiskTier, tiered

    assert "No matching source regions were retrieved." in router.prompts[0]
    assert '<untrusted-content source="selfmod:failed-proposals">' in router.prompts[0]
    assert len(edits) == 1
    assert edits[0].origin_trust is ContentTrust.UNTRUSTED
    assert tiered(edits)[0].risk_tier is RiskTier.REVIEW


def test_multiline_private_key_region_is_omitted(tmp_path, monkeypatch):
    target = _seed(tmp_path)
    target.write_text(
        "def calculate_total():\n"
        "    key = '''-----BEGIN PRIVATE KEY-----\n"
        "    PEM_BODY_99384726\n"
        "    -----END PRIVATE KEY-----'''\n",
        encoding="utf-8",
    )
    router = _Router()

    _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total PRIVATE")

    assert "PEM_BODY_99384726" not in router.prompts[0]
    assert "BEGIN PRIVATE KEY" not in router.prompts[0]


def test_secret_region_disqualifies_other_hits_from_same_file():
    from hive.runtime import _diagnoser_code_context

    class SplitSecretIndex:
        def search_symbol(self, term, **_kwargs):
            if term == "calculate_total":
                return [{
                    "file": "src/hive/calculator.py", "line": 1, "kind": "definition",
                    "context": "1: def calculate_total():\n2:     key = '''-----BEGIN PRIVATE KEY-----",
                }]
            if term == "later_function":
                return [{
                    "file": "src/hive/calculator.py", "line": 8, "kind": "definition",
                    "context": "8: def later_function():\n9:     return 'PEM_BODY_5519823'",
                }]
            return []

        def search_text(self, _term, **_kwargs):
            return []

    rendered, shown = _diagnoser_code_context(
        SplitSecretIndex(), "calculate_total later_function",
    )

    assert rendered == ""
    assert shown == {}


def test_comma_separated_configured_key_component_omits_region(tmp_path, monkeypatch):
    target = _seed(tmp_path)
    component = "second-configured-key-675129"
    target.write_text(
        f"def calculate_total():\n    return '{component}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    monkeypatch.setenv("MINIMAX_API_KEY", f"first-configured-key-124578,{component}")

    _diagnose(hive, monkeypatch, "calculate_total")

    assert component not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


@pytest.mark.parametrize("config_field", [
    "approver_key", "minimax_api_key", "stripe_secret_key",
])
def test_active_config_only_secret_is_omitted(tmp_path, monkeypatch, config_field):
    secret = f"config-only-{config_field}-846271"
    target = _seed(tmp_path)
    target.write_text(
        f"def calculate_total():\n    return '{secret}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, **{config_field: secret})
    from hive.core.config import set_config

    set_config(hive.config)
    assert secret not in known_secret_values()

    _diagnose(hive, monkeypatch, "calculate_total")

    assert secret not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


def test_numbered_context_omits_cropped_multiline_config_secret(tmp_path, monkeypatch):
    first = "config-first-line-537281"
    middle = "config-middle-line-641928"
    last = "config-last-line-865014"
    target = _seed(tmp_path)
    target.write_text(
        "def calculate_total():\n"
        f"    payload = '''{first}\n"
        f"{middle}\n"
        f"{last}'''\n",
        encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=f"{first}\n{middle}\n{last}")

    _diagnose(hive, monkeypatch, "calculate_total")

    assert all(piece not in router.prompts[0] for piece in (first, middle, last))
    assert "No matching source regions were retrieved." in router.prompts[0]


def test_cropped_long_config_secret_line_is_not_rendered(tmp_path, monkeypatch):
    secret = "CONFIG_CROPPED_" + "a" * 700
    target = _seed(tmp_path)
    target.write_text(
        f"def calculate_total():\n    return '{secret}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, "calculate_total")

    assert secret[:100] not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


def test_configured_key_component_in_filename_is_not_rendered(tmp_path, monkeypatch):
    component = "filename-key-component-705193"
    target = _seed(tmp_path, f"src/hive/{component}.py")
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(
        hive.config, minimax_api_key=f"first-key-component-917504,{component}",
    )

    _diagnose(hive, monkeypatch, "calculate_total")

    assert target.exists()
    assert component not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


@pytest.mark.parametrize("trusted", [True, False])
def test_known_secrets_in_symptom_are_masked_without_changing_trust(
    tmp_path, monkeypatch, trusted,
):
    config_secret = "symptom-config-secret-918527"
    env_secret = "symptom-env-secret-643209"
    router = _Router([_edit(
        op="add_test", path="tests/test_new.py", start_line=None,
        end_line=None, old_text="", new_text="def test_new(): pass\n",
    )])
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=config_secret)
    monkeypatch.setenv("HIVE_SECRET", env_secret)
    symptom_text = f"failure involving {config_secret} and {env_secret}"
    symptom = (
        ContentEnvelope.trusted(symptom_text, source="operator") if trusted
        else ContentEnvelope.untrusted(symptom_text, source="web:report")
    )

    edits = _diagnose(hive, monkeypatch, symptom)

    assert config_secret not in router.prompts[0]
    assert env_secret not in router.prompts[0]
    assert "***REDACTED***" in router.prompts[0]
    assert edits[0].origin_trust is (
        ContentTrust.TRUSTED if trusted else ContentTrust.UNTRUSTED
    )
    assert ('<untrusted-content source="web:report">' in router.prompts[0]) is not trusted


def test_known_secret_in_failed_proposal_hint_is_masked(tmp_path, monkeypatch):
    secret = "prior-config-secret-524809"
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)
    monkeypatch.setattr(
        type(hive.self_modifier), "failed_proposals",
        lambda _self, *, limit: [{"title": f"failure {secret}", "stage": "test"}],
    )

    _diagnose(hive, monkeypatch, "no matching source")

    assert secret not in router.prompts[0]
    assert '<untrusted-content source="selfmod:failed-proposals">' in router.prompts[0]


@pytest.mark.parametrize("encode", [
    lambda value: quote(value, safe=""),
    lambda value: quote_plus(value, safe=""),
    lambda value: quote(quote(value, safe=""), safe=""),
])
def test_encoded_config_secret_is_omitted_from_code_and_symptom(
    tmp_path, monkeypatch, encode,
):
    secret = "config/encoded key+918527"
    encoded = encode(secret)
    target = _seed(tmp_path)
    target.write_text(
        f"def calculate_total():\n    return '{encoded}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, f"calculate_total failure with {encoded}")

    prompt = router.prompts[0]
    assert encoded not in prompt
    assert "No matching source regions were retrieved." in prompt
    assert "***REDACTED***" in prompt


@pytest.mark.parametrize("encoded", ["a%2fb", "a%252fb"])
def test_lowercase_percent_hex_secret_is_omitted_from_code_and_symptom(
    tmp_path, monkeypatch, encoded,
):
    from hive.runtime import _secret_bearing_text

    assert _secret_bearing_text(encoded, frozenset({"a/b"}))
    target = _seed(tmp_path)
    target.write_text(
        f"def calculate_total():\n    return '{encoded}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key="a/b")

    _diagnose(hive, monkeypatch, f"calculate_total failure {encoded}")

    assert encoded not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]
    assert "***REDACTED***" in router.prompts[0]


@pytest.mark.parametrize("encoded", ["a%2fb", "a%252fb"])
def test_lowercase_percent_hex_secret_in_path_is_not_rendered(
    tmp_path, monkeypatch, encoded,
):
    _seed(tmp_path, f"src/hive/{encoded}.py")
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key="a/b")

    _diagnose(hive, monkeypatch, "calculate_total")

    assert encoded not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


def _encoded_layers(value: str, depth: int) -> str:
    for _ in range(depth):
        value = quote(value, safe="")
    return value


@pytest.mark.parametrize("depth", [3, 5])
def test_deeply_encoded_config_secret_is_omitted_from_code_and_symptom(
    tmp_path, monkeypatch, depth,
):
    from hive.runtime import _secret_bearing_text

    secret = "deep/credential key+729415"
    encoded = _encoded_layers(secret, depth)
    assert _secret_bearing_text(encoded, frozenset({secret}))
    target = _seed(tmp_path)
    target.write_text(
        f"def calculate_total():\n    return '{encoded}'\n", encoding="utf-8",
    )
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, f"calculate_total failed with {encoded}")

    prompt = router.prompts[0]
    assert encoded not in prompt
    assert "No matching source regions were retrieved." in prompt
    assert "[redacted credential-bearing text]" in prompt


@pytest.mark.parametrize("depth", [3, 5])
def test_deeply_encoded_config_secret_in_path_is_not_rendered(
    tmp_path, monkeypatch, depth,
):
    secret = "deep/credential key+729415"
    encoded = _encoded_layers(secret, depth)
    _seed(tmp_path, f"src/hive/{encoded}.py")
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, "calculate_total")

    assert encoded not in router.prompts[0]
    assert "No matching source regions were retrieved." in router.prompts[0]


@pytest.mark.parametrize("depth", [3, 5])
def test_deeply_encoded_config_secret_is_not_persisted_to_task_board(
    tmp_path, monkeypatch, depth,
):
    from hive.autonomy.tasks import TaskBoard
    from hive.core import spec_search
    from hive.core.spec_search import EditOp, EditOutcome, RiskTier

    secret = "deep/credential key+729415"
    encoded = _encoded_layers(secret, depth)
    hive = _hive(tmp_path, _Router())
    hive.config = replace(hive.config, approver_key=secret)
    symptom = ContentEnvelope.trusted("x" * 190 + encoded, source=f"web:{encoded}")

    async def outcome_without_running_candidate(_diagnoser, _context, _improver):
        return [EditOutcome(
            edit_id="test-review", op=EditOp.PATCH_CODE, tier=RiskTier.REVIEW,
            status="pending_approval", detail=f"needs review: {encoded}",
        )]

    monkeypatch.setattr(spec_search, "diagnose_and_run", outcome_without_running_candidate)
    asyncio.run(hive.self_improve_from_symptom(symptom, _already_enriched=True))

    reopened = TaskBoard(hive.config.state_db)
    try:
        rows = [row for row in reopened.all() if row.kind == "self_improve"]
    finally:
        reopened.close()
    assert len(rows) == 1
    payload = rows[0].payload
    assert encoded not in json.dumps(payload)
    assert payload["symptom"] == "[redacted credential-bearing text]"
    assert payload["origin_source"] == "[redacted credential-bearing text]"
    assert payload["detail"] == "[redacted credential-bearing text]"


def test_deeply_encoded_config_secret_in_history_is_not_prompted(tmp_path, monkeypatch):
    secret = "deep/credential key+729415"
    encoded = quote_plus(secret, safe="")
    for _ in range(4):
        encoded = quote(encoded, safe="")
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)
    monkeypatch.setattr(
        type(hive.self_modifier), "failed_proposals",
        lambda _self, *, limit: [{"title": f"failure {encoded}", "stage": "test"}],
    )

    _diagnose(hive, monkeypatch, "no matching source")

    assert encoded not in router.prompts[0]
    assert "[redacted credential-bearing text]" in router.prompts[0]


@pytest.mark.parametrize("include_encoded_secret", [False, True])
def test_oversized_nested_percent_symptom_is_omitted_before_decoding(
    tmp_path, monkeypatch, include_encoded_secret,
):
    secret = "deep/credential key+729415"
    encoded = _encoded_layers(secret, 5)
    long_symptom = "%2525" * 5000
    if include_encoded_secret:
        long_symptom = "x" * 2995 + encoded + long_symptom
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, long_symptom)

    prompt = router.prompts[0]
    assert "[redacted credential-bearing text]" in prompt
    assert long_symptom[:100] not in prompt
    assert encoded not in prompt
    assert secret not in prompt


def test_secret_hits_do_not_consume_safe_region_limit(tmp_path, monkeypatch):
    secret = "config-only-region-secret-405271"
    for number in range(12):
        name = "alpha" if number < 8 else "beta"
        target = _seed(tmp_path, f"src/hive/a_secret_{number:02}.py")
        target.write_text(
            f"def {name}():\n    return '{secret}'\n", encoding="utf-8",
        )
    safe = _seed(tmp_path, "src/hive/z_safe.py")
    safe.write_text("def beta():\n    return 42\n", encoding="utf-8")
    router = _Router()
    hive = _hive(tmp_path, router)
    hive.config = replace(hive.config, approver_key=secret)

    _diagnose(hive, monkeypatch, "alpha beta")

    prompt = router.prompts[0]
    assert secret not in prompt
    assert '<untrusted-content source="repo:src/hive/z_safe.py">' in prompt
    assert "return 42" in prompt


def test_task_board_persists_redacted_post_diagnosis_metadata(tmp_path, monkeypatch):
    from hive.autonomy.tasks import TaskBoard
    from hive.core import spec_search
    from hive.core.spec_search import EditOp, EditOutcome, RiskTier

    secret = "ZZZSECRET_config-only_510284"
    hive = _hive(tmp_path, _Router())
    hive.config = replace(hive.config, approver_key=secret)
    symptom = ContentEnvelope.trusted("x" * 190 + secret, source=f"web:{secret}")

    async def outcome_without_running_candidate(_diagnoser, _context, _improver):
        return [EditOutcome(
            edit_id="test-review", op=EditOp.PATCH_CODE, tier=RiskTier.REVIEW,
            status="pending_approval", detail=f"needs review: {secret}",
        )]

    monkeypatch.setattr(spec_search, "diagnose_and_run", outcome_without_running_candidate)

    asyncio.run(hive.self_improve_from_symptom(symptom, _already_enriched=True))

    reopened = TaskBoard(hive.config.state_db)
    try:
        rows = [row for row in reopened.all() if row.kind == "self_improve"]
    finally:
        reopened.close()
    assert len(rows) == 1
    payload = rows[0].payload
    assert secret not in json.dumps(payload)
    assert "ZZZSECRET" not in payload["symptom"]
    assert "***REDACTED***" in payload["origin_source"]
    assert "***REDACTED***" in payload["detail"]
    assert payload["tier"] == "review"


def test_fresh_patch_code_without_range_cannot_edit_unseen_python_file(
    tmp_path, monkeypatch,
):
    _seed(tmp_path)
    unseen = _seed(tmp_path, "src/hive/unseen.py")
    router = _Router([_edit(
        op="patch_code", path="src/hive/unseen.py", start_line=None,
        end_line=None,
    )])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    assert edits == []
    assert "return value + 1" in unseen.read_text(encoding="utf-8")


def test_invalid_target_logging_omits_model_supplied_path(
    tmp_path, monkeypatch, caplog,
):
    marker = "SECRET_PATH_MARKER"
    router = _Router([_edit(
        op="edit_docs", path=f"src/hive/{marker}.py", start_line=None,
        end_line=None,
    )])

    assert _diagnose(_hive(tmp_path, router), monkeypatch, "no source") == []
    assert marker not in caplog.text


def test_html_escaped_old_text_maps_back_to_exact_seen_line(tmp_path, monkeypatch):
    target = _seed(tmp_path)
    target.write_text(
        "def calculate_total(value):\n    return value < 10\n", encoding="utf-8",
    )
    router = _Router([_edit(
        old_text="return value &lt; 10", new_text="return value < 11",
    )])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    assert "return value &lt; 10" in router.prompts[0]
    assert len(edits) == 1
    candidate = tmp_path / "candidate"
    dest = _seed(candidate)
    dest.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    assert asyncio.run(edits[0].apply(str(candidate))) == ["src/hive/calculator.py"]
    assert "return value < 11" in dest.read_text(encoding="utf-8")


def test_malformed_new_text_skips_only_that_proposal(tmp_path, monkeypatch):
    _seed(tmp_path)
    router = _Router([_edit(new_text=["not", "text"]), _edit()])

    edits = _diagnose(_hive(tmp_path, router), monkeypatch, "calculate_total")

    assert len(edits) == 1
    assert edits[0].code == "return value + 2"


@pytest.mark.parametrize("failure", [
    "escape", "create_exists", "create_syntax", "patch_syntax", "old_text_absent",
])
def test_apply_failure_logs_never_echo_model_path(tmp_path, monkeypatch, caplog, failure):
    marker = "SECRET_PATH_MARKER_52761"
    if failure == "escape":
        proposal = _edit(
            op="create_file", path=f"../{marker}.py", old_text="",
            new_text="pass\n", start_line=None, end_line=None,
        )
    elif failure.startswith("create_"):
        proposal = _edit(
            op="create_file", path=f"tests/{marker}.py", old_text="",
            new_text="def broken(:\n" if failure == "create_syntax" else "pass\n",
            start_line=None, end_line=None,
        )
    elif failure == "old_text_absent":
        proposal = _edit(
            op="patch_code", path=f"docs/{marker}.md", old_text="old",
            new_text="new", start_line=None, end_line=None,
        )
    else:
        path = f"src/hive/{marker}.py"
        _seed(tmp_path, path)
        proposal = _edit(
            path=path,
            old_text="return value + 1",
            new_text="return value +" if failure == "patch_syntax" else "return value + 2",
        )

    edits = _diagnose(_hive(tmp_path, _Router([proposal])), monkeypatch, "calculate_total")
    assert len(edits) == 1
    candidate = tmp_path / "candidate"
    if failure == "create_exists":
        _seed(candidate, proposal["path"])
    if failure == "patch_syntax":
        _seed(candidate, proposal["path"])
    if failure == "old_text_absent":
        dest = candidate / proposal["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("different", encoding="utf-8")
    caplog.clear()

    assert asyncio.run(edits[0].apply(str(candidate))) == []
    assert marker not in caplog.text
