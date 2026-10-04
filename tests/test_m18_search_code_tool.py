"""M18: Hive reads bounded repo code without persisting source or query text."""

from __future__ import annotations

import asyncio
import json

from hive.core.types import ContentTrust
from hive.tools.builtins import SearchCode, register_builtins
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.registry import ToolRegistry


def _repo(tmp_path):
    source = tmp_path / "src" / "hive"
    source.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (source / "example.py").write_text(
        "def find_target():\n    return 1\n\nfind_target()\n", encoding="utf-8",
    )
    return tmp_path


def test_search_code_registered_read_only_and_available_in_repo(tmp_path):
    class Registry(ToolRegistry):
        pass

    tools = register_builtins(Registry)
    assert "search_code" in tools
    assert tools["search_code"].spec.dangerous is False
    assert SearchCode(root=_repo(tmp_path)).available()
    assert not SearchCode(root=tmp_path / "missing").available()


def test_search_code_text_and_symbol_results_are_bounded_untrusted(tmp_path):
    tool = SearchCode(root=_repo(tmp_path))
    text_result = asyncio.run(tool.execute(query="return 1", mode="text", limit=2))
    assert text_result.success
    assert text_result.envelope.trust is ContentTrust.UNTRUSTED
    text_payload = json.loads(text_result.content)
    assert text_payload["mode"] == "text"
    assert text_payload["results"][0]["file"] == "src/hive/example.py"
    assert text_payload["results"][0]["line"] == 2
    assert "return 1" in text_payload["results"][0]["context"]

    symbol_result = asyncio.run(tool.execute(query="find_target", mode="symbol"))
    symbol_payload = json.loads(symbol_result.content)
    assert symbol_payload["mode"] == "symbol"
    assert {(item["kind"], item["line"]) for item in symbol_payload["results"]} == {
        ("definition", 1), ("call", 4),
    }


def test_search_code_finds_real_hiveos_definition():
    tool = SearchCode()
    assert tool.available()
    result = asyncio.run(tool.execute(query="CodeIndex", mode="symbol", limit=20))
    assert result.success
    assert any(
        item["file"] == "src/hive/tools/code_search.py"
        and item["kind"] == "definition"
        and item["line"] > 0
        for item in json.loads(result.content)["results"]
    )


def test_symbol_definition_survives_call_site_result_limit(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "hive" / "example.py").write_text(
        "\n".join("find_target()" for _ in range(20))
        + "\ndef find_target():\n    pass\n",
        encoding="utf-8",
    )
    payload = json.loads(asyncio.run(SearchCode(root=root).execute(
        query="find_target", mode="symbol", limit=1,
    )).content)
    assert payload["results"][0]["kind"] == "definition"
    assert payload["results"][0]["line"] == 21


def test_long_line_context_contains_the_match(tmp_path):
    root = _repo(tmp_path)
    marker = "FIND_THIS_MARKER"
    (root / "src" / "hive" / "example.py").write_text(
        "# " + "x" * 300 + marker + "\n", encoding="utf-8",
    )
    payload = json.loads(asyncio.run(SearchCode(root=root).execute(
        query=marker, mode="text", limit=1, context_lines=0,
    )).content)
    assert marker in payload["results"][0]["context"]


def test_unicode_casefold_offset_does_not_hide_text_match(tmp_path):
    root = _repo(tmp_path)
    marker = "FIND_THIS_MARKER"
    (root / "src" / "hive" / "example.py").write_text(
        "# " + "ß" * 300 + marker + "x" * 500 + "\n", encoding="utf-8",
    )
    payload = json.loads(asyncio.run(SearchCode(root=root).execute(
        query=marker, mode="text", limit=1, context_lines=0,
    )).content)
    assert marker in payload["results"][0]["context"]


def test_search_code_query_and_snippets_never_enter_audit(tmp_path):
    root = _repo(tmp_path)
    marker = "PRIVATE-CODE-QUERY-DO-NOT-AUDIT"
    (root / "src" / "hive" / "example.py").write_text(
        f"def find_target():\n    return '{marker}'\n", encoding="utf-8",
    )
    tool = SearchCode(root=root)
    audit = []
    executor = ToolExecutor({tool.spec.name: tool}, audit=audit.append)
    dispatch = asyncio.run(executor.execute("search_code", {"query": marker, "mode": "text"}))
    assert dispatch.status is DispatchStatus.OK
    assert marker in dispatch.result.content
    assert marker not in json.dumps(audit)
    assert audit[0]["result"] == "[repository code omitted]"
    assert audit[0]["args"] == {"mode": "text", "query_length": len(marker),
                                "limit": 10, "context_lines": 1}


def test_search_code_rejects_unbounded_or_malformed_queries_before_index(tmp_path,
                                                                          monkeypatch):
    tool = SearchCode(root=_repo(tmp_path))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid input reached index")

    monkeypatch.setattr(tool._index, "search_text", forbidden)
    monkeypatch.setattr(tool._index, "search_symbol", forbidden)
    for params in ({"query": ""}, {"query": "x" * 121}, {"query": "x\nsecret"},
                   {"query": "x", "mode": "regex"}, {"query": "x", "limit": 0},
                   {"query": "x", "limit": True}, {"query": "x", "context_lines": 3}):
        assert not asyncio.run(tool.execute(**params)).success
