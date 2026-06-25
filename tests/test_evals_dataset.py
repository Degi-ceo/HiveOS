"""Tests for hive.evals.dataset — JSONL + YAML loaders."""
from __future__ import annotations

import json
import textwrap

import pytest

from hive.evals.dataset import (
    DatasetError,
    load,
    load_jsonl,
    load_many,
    load_yaml,
)


# ---------- JSONL ----------------------------------------------------------------

def test_load_jsonl_minimal(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text(json.dumps({"id": "a", "input": "x", "expected": "y", "grader": "exact"}) + "\n")
    items = load_jsonl(p)
    assert len(items) == 1
    it = items[0]
    assert it.id == "a" and it.input == "x" and it.expected == "y"
    assert it.grader == "exact" and it.extra == {}


def test_load_jsonl_skips_blanks_and_comments(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text(textwrap.dedent("""\
        # header comment

        {"id": "a", "input": "x", "expected": "y", "grader": "exact"}

        # another comment
        {"id": "b", "input": "x", "expected": "y", "grader": "exact"}
    """))
    items = load_jsonl(p)
    assert [i.id for i in items] == ["a", "b"]


def test_load_jsonl_with_extra(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text(json.dumps({
        "id": "r", "input": "x", "expected": "y", "grader": "regex",
        "extra": {"flags": "i", "pattern": "[a-z]+"},
    }) + "\n")
    items = load_jsonl(p)
    assert items[0].extra == {"flags": "i", "pattern": "[a-z]+"}


def test_load_jsonl_malformed_json_raises(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('{"id": "a", "input": "x",\n')
    with pytest.raises(DatasetError) as exc:
        load_jsonl(p)
    assert "invalid JSON" in str(exc.value)
    assert "line 1" in str(exc.value)


def test_load_jsonl_non_object_raises(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('[1, 2, 3]\n')
    with pytest.raises(DatasetError) as exc:
        load_jsonl(p)
    assert "expected object" in str(exc.value)


def test_load_jsonl_missing_keys_raises(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('{"id": "a", "input": "x"}\n')
    with pytest.raises(DatasetError) as exc:
        load_jsonl(p)
    assert "missing required keys" in str(exc.value)
    assert "expected" in str(exc.value)
    assert "grader" in str(exc.value)


def test_load_jsonl_extra_not_dict_raises(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('{"id": "a", "input": "x", "expected": "y", "grader": "exact", "extra": "oops"}\n')
    with pytest.raises(DatasetError) as exc:
        load_jsonl(p)
    assert "'extra' must be a dict" in str(exc.value)


def test_load_jsonl_extra_null_becomes_empty_dict(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('{"id": "a", "input": "x", "expected": "y", "grader": "exact", "extra": null}\n')
    items = load_jsonl(p)
    assert items[0].extra == {}


def test_load_jsonl_error_includes_line_number(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text('{"id": "a", "input": "x", "expected": "y", "grader": "exact"}\n'
                 'not valid json\n')
    with pytest.raises(DatasetError) as exc:
        load_jsonl(p)
    assert ":2:" in str(exc.value)


# ---------- YAML -----------------------------------------------------------------

yaml = pytest.importorskip("yaml", reason="PyYAML not installed")


def test_load_yaml_basic(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text(textwrap.dedent("""\
        - id: a
          input: hi
          expected: hello
          grader: exact
        - id: b
          input: bye
          expected: goodbye
          grader: exact
    """))
    items = load_yaml(p)
    assert len(items) == 2
    assert items[0].id == "a" and items[1].expected == "goodbye"


def test_load_yaml_yml_suffix(tmp_path):
    p = tmp_path / "ds.yml"
    p.write_text("- id: a\n  input: x\n  expected: y\n  grader: exact\n")
    assert load_yaml(p)[0].id == "a"


def test_load_yaml_not_a_list_raises(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text("key: value\n")
    with pytest.raises(DatasetError) as exc:
        load_yaml(p)
    assert "expected a YAML list" in str(exc.value)


def test_load_yaml_item_missing_keys(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text("- id: a\n  input: x\n")
    with pytest.raises(DatasetError) as exc:
        load_yaml(p)
    assert "missing required keys" in str(exc.value)


# ---------- dispatch -------------------------------------------------------------

def test_load_dispatches_by_suffix(tmp_path):
    p_jsonl = tmp_path / "a.jsonl"
    p_jsonl.write_text('{"id": "a", "input": "x", "expected": "y", "grader": "exact"}\n')
    p_yaml = tmp_path / "b.yaml"
    p_yaml.write_text("- id: b\n  input: x\n  expected: y\n  grader: exact\n")
    assert load(p_jsonl)[0].id == "a"
    assert load(p_yaml)[0].id == "b"
    p_yml = tmp_path / "c.yml"
    p_yml.write_text("- id: c\n  input: x\n  expected: y\n  grader: exact\n")
    assert load(p_yml)[0].id == "c"


def test_load_many_concatenates(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text('{"id": "a", "input": "x", "expected": "y", "grader": "exact"}\n')
    b = tmp_path / "b.jsonl"
    b.write_text('{"id": "b", "input": "x", "expected": "y", "grader": "exact"}\n')
    items = load_many([a, b])
    assert [i.id for i in items] == ["a", "b"]


def test_load_many_empty_list():
    assert load_many([]) == []


def test_load_many_propagates_dataset_error(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text('not json\n')
    with pytest.raises(DatasetError):
        load_many([a])


def test_load_yaml_missing_pyyaml(tmp_path, monkeypatch):
    """If PyYAML isn't installed, load_yaml raises a clear DatasetError
    explaining how to fix it (instead of an opaque ImportError)."""
    p = tmp_path / "ds.yaml"
    p.write_text("- id: a\n  input: x\n  expected: y\n  grader: exact\n")
    # Block the yaml import: any attempt to `import yaml` raises ImportError.
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("simulated: no PyYAML")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Also clear any cached yaml module so the import runs.
    import sys
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    with pytest.raises(DatasetError) as exc:
        load_yaml(p)
    assert "PyYAML" in str(exc.value) or "yaml" in str(exc.value).lower()
