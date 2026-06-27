"""test_introspect.py — full coverage + acceptance for src/hive/tools/introspect.py.

SPRINT_6 P-H: AST-based self-introspection of Hive's tool surface (issue #76).

Coverage targets:
  src/hive/tools/introspect.py   -> 100%
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hive.tools.introspect import (
    _is_basetool_subclass,
    _class_assign_strs,
    _extract_tool_from_class,
    _index_module,
    _score,
    _tokenize,
    _walk_sources,
    format_for_discover,
    index,
    search,
)


# ===========================================================================
# Tokenizer (CamelCase / snake_case)
# ===========================================================================

def test_tokenize_splits_camelcase():
    tokens = _tokenize("GitHubListPRs")
    assert "git" in tokens and "hub" in tokens and "list" in tokens


def test_tokenize_splits_snake_case():
    assert _tokenize("github_list_prs") == {"github", "list", "prs"}


def test_tokenize_splits_on_punctuation():
    assert _tokenize("hello, world!") == {"hello", "world"}


def test_tokenize_handles_uppercase_acronym_runs():
    assert _tokenize("XMLParser") == {"xml", "parser"}


def test_tokenize_returns_empty_for_non_alnum():
    assert _tokenize("---") == set()


def test_tokenize_handles_mixed_case_with_lowercase_prefix():
    tokens = _tokenize("readFile")
    assert "read" in tokens and "file" in tokens


# ===========================================================================
# AST classifier + extractor
# ===========================================================================

def test_is_basetool_subclass_true_for_basetool():
    import ast
    tree = ast.parse("class Foo(BaseTool): pass")
    assert _is_basetool_subclass(tree.body[0]) is True


def test_is_basetool_subclass_true_for_gated_subclass():
    import ast
    tree = ast.parse("class Foo(_Gated): pass")
    assert _is_basetool_subclass(tree.body[0]) is True


def test_is_basetool_subclass_true_for_github_base_subclass():
    import ast
    tree = ast.parse("class Foo(_GitHubBase): pass")
    assert _is_basetool_subclass(tree.body[0]) is True


def test_is_basetool_subclass_false_for_unrelated_class():
    import ast
    tree = ast.parse("class Foo: pass")
    assert _is_basetool_subclass(tree.body[0]) is False


def test_class_assign_strs_lifts_string_attributes():
    import ast
    tree = ast.parse('class Foo:\n    _name = "bar"\n    _desc = "baz"')
    assert _class_assign_strs(tree.body[0]) == {"_name": "bar", "_desc": "baz"}


def test_class_assign_strs_extracts_tool_spec_kwargs():
    import ast
    src = ('class Foo(BaseTool):\n'
           '    spec = ToolSpec(name="bar", description="Baz.", category="cat")\n')
    tree = ast.parse(src)
    assert _class_assign_strs(tree.body[0]) == {
        "name": "bar", "description": "Baz.", "category": "cat"}


def test_class_assign_strs_ignores_tool_spec_dict_kwargs():
    import ast
    src = ('class Foo(BaseTool):\n'
           '    spec = ToolSpec(name="bar", parameters={"type": "object"})\n')
    attrs = _class_assign_strs(ast.parse(src).body[0])
    assert attrs == {"name": "bar"}


def test_extract_tool_from_class_returns_none_for_non_basetool():
    import ast
    tree = ast.parse("class Foo:\n    pass")
    assert _extract_tool_from_class(tree.body[0], "mod") is None


def test_extract_tool_from_class_uses_class_name_when_no_spec():
    import ast
    src = 'class Foo(BaseTool):\n    """A docstring."""\n'
    entry = _extract_tool_from_class(ast.parse(src).body[0], "mod")
    assert entry["name"] == "Foo"
    assert entry["class"] == "Foo"
    assert entry["module"] == "mod"
    assert entry["doc"] == "A docstring."
    assert entry["description"] == "A docstring."
    assert entry["category"] == ""


def test_extract_tool_from_class_prefers_tool_spec_fields():
    import ast
    src = ('class Foo(BaseTool):\n'
           '    spec = ToolSpec(name="bar", description="Baz.", category="cat")\n')
    entry = _extract_tool_from_class(ast.parse(src).body[0], "mod")
    assert entry["name"] == "bar"
    assert entry["description"] == "Baz."
    assert entry["category"] == "cat"


def test_extract_tool_from_class_falls_back_to_underscore_attrs():
    import ast
    src = ('class Foo(_Gated):\n'
           '    _name = "bar"\n'
           '    _desc = "Baz."\n')
    entry = _extract_tool_from_class(ast.parse(src).body[0], "mod")
    assert entry["name"] == "bar"
    assert entry["description"] == "Baz."


# ===========================================================================
# Module indexer — happy path
# ===========================================================================

def test_index_module_handles_valid_module(tmp_path):
    src = ('from hive.tools.base import BaseTool, ToolSpec\n'
           'class Foo(BaseTool):\n'
           '    spec = ToolSpec(name="foo", description="F.")\n'
           '    async def execute(self, **kw): pass\n'
           'class Bar(BaseTool):\n'
           '    spec = ToolSpec(name="bar", description="B.")\n'
           '    async def execute(self, **kw): pass\n'
           'class NotATool:\n    pass\n')
    p = tmp_path / "fake.py"
    p.write_text(src)
    entries = _index_module(p)
    assert len(entries) == 2
    names = {e["name"] for e in entries}
    assert names == {"foo", "bar"}


def test_index_module_skips_malformed_with_warning(tmp_path, caplog):
    p = tmp_path / "broken.py"
    p.write_text("def :\n  class syntax error\n")
    with caplog.at_level(logging.WARNING, logger="hive.tools.introspect"):
        result = _index_module(p)
    assert result == []
    assert "malformed" in caplog.text.lower() or "broken.py" in caplog.text


def test_index_module_returns_empty_for_missing_file(tmp_path, caplog):
    missing = tmp_path / "absent.py"
    with caplog.at_level(logging.WARNING, logger="hive.tools.introspect"):
        result = _index_module(missing)
    assert result == []


def test_walk_sources_returns_sorted_unique_paths(tmp_path):
    (tmp_path / "a.py").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("")
    paths = list(_walk_sources((tmp_path,)))
    names = sorted(p.name for p in paths)
    assert names == ["a.py", "b.py"]


def test_walk_sources_skips_missing_roots(tmp_path):
    missing = tmp_path / "ghost"
    paths = list(_walk_sources((missing,)))
    assert paths == []


def test_walk_sources_dedupes_overlapping_roots(tmp_path):
    (tmp_path / "a.py").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("")
    paths = list(_walk_sources((tmp_path, sub)))
    # The file under tmp_path/sub appears in both roots — should appear once.
    assert len(paths) == len(set(paths))
    assert len(paths) == 2


# ===========================================================================
# Live index — acceptance
# ===========================================================================

def test_live_index_returns_at_least_minimum_tools():
    """Acceptance: index() returns >= minimum BaseTool subclass count."""
    entries = index()
    assert len(entries) >= 22  # 22 BaseTool subclasses in builtins + 1 MCPTool


def test_live_index_is_sorted_by_module_then_class():
    entries = index()
    keys = [(e["module"], e["class"]) for e in entries]
    assert keys == sorted(keys)


def test_live_index_includes_mcp_tool():
    entries = index()
    assert any(e["class"] == "MCPTool" and e["module"] == "client" for e in entries)


def test_live_index_excludes_non_basetool_classes():
    """A non-BaseTool class accidentally present in builtins must not appear."""
    entries = index()
    classes = {e["class"] for e in entries}
    # ToolRegistry / ToolSpec / etc. should never be picked up.
    assert "ToolRegistry" not in classes
    assert "ToolSpec" not in classes
    assert "BaseTool" not in classes


# ===========================================================================
# Scoring
# ===========================================================================

def test_score_zero_for_empty_query():
    assert _score({"name": "foo", "description": "bar"}, set()) == 0.0


def test_score_zero_for_empty_hay():
    assert _score({"name": "", "description": "", "doc": "", "class": ""},
                  {"foo"}) == 0.0


def test_score_exact_token_match():
    entry = {"name": "github_list_prs",
             "description": "List open pull requests.",
             "doc": "", "class": "GitHubListPRs"}
    score = _score(entry, {"list"})
    assert score == 1.0


def test_score_prefix_match():
    entry = {"name": "github_list_prs",
             "description": "List open pull requests.",
             "doc": "", "class": "GitHubListPRs"}
    score = _score(entry, {"github", "missing"})
    assert score == 0.5


def test_score_substring_against_name_field():
    """Raw substring in name field counts as a hit even when tokenizer splits."""
    entry = {"name": "deploy", "description": "", "doc": "", "class": "Deploy"}
    score = _score(entry, {"deploying", "irrelevant"})
    assert score == 0.5


def test_score_short_tokens_no_match():
    entry = {"name": "x", "description": "", "doc": "", "class": "X"}
    assert _score(entry, {"a", "b", "c"}) == 0.0


# ===========================================================================
# Search — acceptance
# ===========================================================================

def test_search_returns_top_hit_for_github_pr_list():
    """Acceptance: 'github pr list' returns GitHubListPRs with score > 0.8."""
    results = search("github pr list", k=5)
    assert results, "expected at least one result"
    top = results[0]
    assert top["class"] == "GitHubListPRs"
    assert top["source"] == "ast"
    assert top["score"] > 0.8


def test_search_attaches_source_ast_to_every_hit():
    for hit in search("github pr list"):
        assert hit["source"] == "ast"


def test_search_returns_empty_for_zero_match_query():
    results = search("zzzqqqxxxnomatch", k=5)
    assert results == []


def test_search_empty_query_returns_nothing():
    results = search("", k=5)
    assert results == []


def test_search_is_deterministic():
    """Same query → identical ordered results."""
    r1 = search("github pr list", k=5)
    r2 = search("github pr list", k=5)
    assert r1 == r2


def test_search_honors_k_limit():
    results = search("tool", k=2)
    assert len(results) <= 2


def test_search_results_sorted_by_score_then_module_then_class():
    results = search("github pr list", k=5)
    keys = [(-r["score"], r["module"], r["class"]) for r in results]
    assert keys == sorted(keys)


# ===========================================================================
# format_for_discover
# ===========================================================================

def test_format_for_discover_shapes_results_with_source_and_score():
    hits = [{"source": "ast", "name": "github_list_prs", "class": "GitHubListPRs",
             "module": "__init__", "description": "List PRs.", "score": 1.0}]
    out = format_for_discover(hits)
    assert out == [{"source": "ast", "name": "github_list_prs",
                    "class": "GitHubListPRs", "module": "__init__",
                    "description": "List PRs.", "score": 1.0}]


def test_format_for_discover_handles_missing_keys():
    out = format_for_discover([{}])
    assert out == [{"source": "ast", "name": "", "class": "", "module": "",
                    "description": "", "score": 0.0}]


def test_format_for_discover_empty_list():
    assert format_for_discover([]) == []


# ===========================================================================
# Custom roots — index() and search() accept caller-supplied roots
# ===========================================================================

def test_index_with_custom_roots_only(tmp_path):
    src = ('from hive.tools.base import BaseTool\n'
           'class FakeTool(BaseTool):\n'
           '    """My fake tool."""\n'
           '    spec = "ignored"\n'
           '    async def execute(self, **kw): pass\n')
    root = tmp_path / "fake_mod"
    root.mkdir()
    (root / "fake.py").write_text(src)
    entries = index(roots=(root,))
    assert len(entries) == 1
    assert entries[0]["class"] == "FakeTool"
    assert entries[0]["description"] == "My fake tool."


def test_search_uses_provided_idx_without_reindexing():
    entries = index()
    # Pass a custom idx with one entry; search must NOT call index() itself.
    custom = [{"name": "needle", "module": "m", "class": "Needle",
               "doc": "needle in a haystack", "description": "",
               "category": ""}]
    results = search("needle", idx=custom)
    assert results and results[0]["class"] == "Needle"
