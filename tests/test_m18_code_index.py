"""Contract tests for the bounded, in-memory source index."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hive.tools.code_search as code_search
from hive.tools.code_search import CodeIndex


def _source(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _directory_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            pytest.skip("directory junction creation unavailable")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlink unavailable: {exc}")


def _replace_with_link(directory: Path, target: Path, fixture_root: Path) -> None:
    parked = directory.with_name(directory.name + "_parked")
    assert directory.resolve().is_relative_to(fixture_root.resolve())
    assert parked.parent.resolve().is_relative_to(fixture_root.resolve())
    directory.rename(parked)
    _directory_link(directory, target)


def test_text_search_is_limited_to_python_in_allowed_trees(tmp_path):
    _source(tmp_path, "src/hive/a.py", "# Needle here\n")
    _source(tmp_path, "tests/test_a.py", "# needle again\n")
    _source(tmp_path, "src/hive/a.txt", "needle in text file\n")
    _source(tmp_path, "docs/a.py", "needle in docs\n")
    _source(tmp_path, "other/a.py", "needle elsewhere\n")

    hits = CodeIndex(tmp_path).search_text("needle")

    assert [(hit["file"], hit["line"]) for hit in hits] == [
        ("src/hive/a.py", 1),
        ("tests/test_a.py", 1),
    ]
    assert all("needle" in hit["context"].casefold() for hit in hits)


def test_symbol_search_returns_definitions_and_calls_in_source_order(tmp_path):
    _source(
        tmp_path,
        "src/hive/worker.py",
        "class Worker:\n"
        "    async def run(self):\n"
        "        return self.run()\n"
        "def run():\n"
        "    Worker().run()\n",
    )
    index = CodeIndex(tmp_path)

    assert [(hit["line"], hit["kind"], hit["symbol"]) for hit in index.search_symbol("run")] == [
        (2, "definition", "run"),
        (3, "call", "run"),
        (4, "definition", "run"),
        (5, "call", "run"),
    ]
    assert [(hit["line"], hit["kind"]) for hit in index.search_symbol("Worker")] == [
        (1, "definition"),
        (5, "call"),
    ]
    assert all(hit["file"] == "src/hive/worker.py" for hit in index.search_symbol("run"))


def test_symbol_limit_keeps_later_definition_despite_earlier_calls(tmp_path):
    _source(tmp_path, "src/hive/a.py", "find_target()\nfind_target()\nfind_target()\n")
    _source(tmp_path, "src/hive/z.py", "def find_target():\n    pass\n")
    index = CodeIndex(tmp_path)

    assert [(hit["file"], hit["kind"]) for hit in index.search_symbol("find_target", limit=2)] == [
        ("src/hive/a.py", "call"),
        ("src/hive/z.py", "definition"),
    ]
    only = index.search_symbol("find_target", limit=1)
    assert [(hit["file"], hit["kind"]) for hit in only] == [
        ("src/hive/z.py", "definition"),
    ]


def test_results_are_deterministic_and_bounds_are_enforced(tmp_path):
    _source(tmp_path, "tests/z.py", "needle\nneedle\n")
    _source(tmp_path, "src/hive/a.py", "before\nneedle\nafter\n")
    index = CodeIndex(tmp_path)

    first = index.search_text("needle", limit=2, context_lines=1)
    assert first == index.search_text("needle", limit=2, context_lines=1)
    assert [(hit["file"], hit["line"]) for hit in first] == [
        ("src/hive/a.py", 2),
        ("tests/z.py", 1),
    ]
    assert "before" in first[0]["context"]
    assert "after" in first[0]["context"]
    assert index.search_text("needle", limit=0) == []
    assert index.search_text("") == []
    assert len(index.search_text("needle", context_lines=999)[0]["context"].splitlines()) <= 11


def test_context_and_query_are_bounded(tmp_path):
    _source(tmp_path, "src/hive/long.py", "needle" + "x" * 10000 + "\n")
    index = CodeIndex(tmp_path)

    hits = index.search_text("needle")
    assert len(hits) == 1
    assert len(hits[0]["context"]) < 3000
    assert index.search_text("x" * 300) == []


def test_text_context_window_includes_match_beyond_first_240_columns(tmp_path):
    _source(tmp_path, "src/hive/long.py", "x" * 500 + "NEEDLE" + "y" * 500 + "\n")

    hit = CodeIndex(tmp_path).search_text("needle", context_lines=0)[0]

    assert "NEEDLE" in hit["context"]
    assert len(hit["context"]) <= 300


def test_text_context_maps_casefold_expansion_to_original_column(tmp_path):
    _source(tmp_path, "src/hive/unicode.py", "ß" * 300 + "NEEDLE" + "x" * 500 + "\n")

    hit = CodeIndex(tmp_path).search_text("needle", context_lines=0)[0]

    assert "NEEDLE" in hit["context"]
    assert len(hit["context"]) <= 300


def test_symbol_context_window_includes_late_definition_and_call(tmp_path):
    indentation = " " * 300
    _source(
        tmp_path,
        "src/hive/long_symbols.py",
        "class Holder:\n"
        f"{indentation}def find_target(self):\n"
        f"{indentation}    value = '{'x' * 300}'; self.find_target()\n",
    )

    hits = CodeIndex(tmp_path).search_symbol("find_target", context_lines=0)

    assert [(hit["line"], hit["kind"]) for hit in hits] == [
        (2, "definition"),
        (3, "call"),
    ]
    assert all("find_target" in hit["context"] for hit in hits)
    assert all(len(hit["context"]) <= 300 for hit in hits)


def test_oversized_and_invalid_python_are_handled_without_unbounded_parse(tmp_path):
    _source(tmp_path, "src/hive/huge.py", "needle" + "x" * 1_100_000)
    _source(tmp_path, "tests/test_broken.py", "needle = (\n")
    index = CodeIndex(tmp_path)

    assert index.search_text("needle") == [
        {"file": "tests/test_broken.py", "line": 1, "context": "1: needle = ("},
    ]
    assert index.search_symbol("needle") == []


def test_unchanged_files_are_not_reparsed_between_queries(tmp_path, monkeypatch):
    _source(tmp_path, "src/hive/worker.py", "def work():\n    return 1\nwork()\n")
    index = CodeIndex(tmp_path)
    real_parse = ast.parse
    calls = []

    def counted_parse(*args, **kwargs):
        calls.append(args[1] if len(args) > 1 else kwargs.get("filename"))
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted_parse)
    assert len(index.search_symbol("work")) == 2
    assert index.search_text("return")
    assert len(index.search_symbol("work")) == 2
    assert len(calls) == 1


def test_ordinary_repeat_query_avoids_walk_and_source_reads(tmp_path, monkeypatch):
    _source(tmp_path, "src/hive/worker.py", "def find_target():\n    pass\nfind_target()\n")
    index = CodeIndex(tmp_path)
    assert len(index.search_symbol("find_target")) == 2
    real_scandir = os.scandir
    real_open = os.open
    calls = {"scan": 0, "read": 0}

    def counted_scandir(*args, **kwargs):
        calls["scan"] += 1
        return real_scandir(*args, **kwargs)

    def counted_open(*args, **kwargs):
        calls["read"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(code_search.os, "scandir", counted_scandir)
    monkeypatch.setattr(code_search.os, "open", counted_open)
    assert len(index.search_symbol("find_target")) == 2
    assert index.search_text("pass")
    assert calls == {"scan": 0, "read": 0}


def test_concurrent_queries_share_one_consistent_cache(tmp_path, monkeypatch):
    _source(tmp_path, "src/hive/worker.py", "def find_target():\n    pass\n\nfind_target()\n")
    index = CodeIndex(tmp_path)
    real_parse = ast.parse
    calls = []

    def counted_parse(*args, **kwargs):
        calls.append(1)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted_parse)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: index.search_symbol("find_target"), range(32)))

    assert all([(hit["line"], hit["kind"]) for hit in result] == [
        (1, "definition"),
        (4, "call"),
    ] for result in results)
    assert len(calls) == 1


def test_changed_file_refreshes_text_and_symbol_index(tmp_path):
    path = _source(tmp_path, "src/hive/worker.py", "def old_name():\n    pass\n")
    index = CodeIndex(tmp_path)
    assert len(index.search_symbol("old_name")) == 1

    path.write_text("def new_name():\n    new_name()\n", encoding="utf-8")
    index.refresh(force=True)

    assert index.search_symbol("old_name") == []
    assert [(hit["line"], hit["kind"]) for hit in index.search_symbol("new_name")] == [
        (1, "definition"),
        (2, "call"),
    ]
    assert index.search_text("old_name") == []


def test_same_size_replacement_with_restored_mtime_invalidates_cache(tmp_path):
    path = _source(tmp_path, "src/hive/worker.py", "def old_name():\n    pass\n")
    index = CodeIndex(tmp_path)
    assert index.search_symbol("old_name")
    original = path.stat()

    path.write_text("def new_name():\n    pass\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    changed = path.stat()
    assert changed.st_size == original.st_size
    assert changed.st_mtime_ns == original.st_mtime_ns
    index.refresh(force=True)

    assert index.search_symbol("old_name") == []
    assert [(hit["line"], hit["kind"]) for hit in index.search_symbol("new_name")] == [
        (1, "definition"),
    ]


def test_periodic_refresh_detects_same_mtime_replacement_after_staleness_window(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    path = _source(tmp_path, "src/hive/worker.py", "def old_name():\n    pass\n")
    index = CodeIndex(tmp_path)
    assert index.search_symbol("old_name")
    original = path.stat()
    path.write_text("def new_name():\n    pass\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert index.search_symbol("old_name")  # Cached until the refresh deadline.
    clock[0] = 10.0

    assert index.search_symbol("old_name") == []
    assert index.search_symbol("new_name")


def test_periodic_manifest_refresh_detects_added_and_deleted_files(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    old = _source(tmp_path, "tests/test_old.py", "marker\n")
    index = CodeIndex(tmp_path)
    assert [(hit["file"], hit["line"]) for hit in index.search_text("marker")] == [
        ("tests/test_old.py", 1),
    ]

    old.unlink()
    _source(tmp_path, "tests/test_new.py", "marker\n")
    clock[0] = 10.0

    assert [(hit["file"], hit["line"]) for hit in index.search_text("marker")] == [
        ("tests/test_new.py", 1),
    ]


def test_deleted_file_is_removed_from_cache(tmp_path):
    path = _source(tmp_path, "tests/test_removed.py", "def gone():\n    pass\n")
    index = CodeIndex(tmp_path)
    assert index.search_symbol("gone")
    path.unlink()
    index.refresh(force=True)

    assert index.search_symbol("gone") == []
    assert index.search_text("gone") == []


def test_file_symlink_escape_is_never_read(tmp_path):
    repo = tmp_path / "repo"
    outside = _source(tmp_path, "outside.py", "SECRET_OUTSIDE\n")
    inside = repo / "src/hive/link.py"
    inside.parent.mkdir(parents=True)
    try:
        inside.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    assert CodeIndex(repo).search_text("SECRET_OUTSIDE") == []


def test_directory_symlink_escape_is_never_traversed(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _source(outside, "hidden.py", "SECRET_OUTSIDE\n")
    inside = repo / "tests"
    repo.mkdir()
    try:
        inside.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    assert CodeIndex(repo).search_text("SECRET_OUTSIDE") == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junction")
def test_windows_junction_escape_is_never_traversed(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _source(outside, "hidden.py", "SECRET_OUTSIDE\n")
    link = repo / "tests" / "linked"
    link.parent.mkdir(parents=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("directory junction creation unavailable")

    assert os.path.isjunction(link)
    assert CodeIndex(repo).search_text("SECRET_OUTSIDE") == []


def test_intermediate_link_to_same_repo_docs_is_rejected_by_loader(tmp_path):
    repo = tmp_path / "repo"
    _source(repo, "docs/note.py", "SECRET_INSIDE_REPO\n")
    alias = repo / "src/hive/alias"
    alias.parent.mkdir(parents=True)
    _directory_link(alias, repo / "docs")
    index = CodeIndex(repo)

    assert index._load("src/hive/alias/note.py", alias / "note.py") is None
    assert index.search_text("SECRET_INSIDE_REPO") == []


def test_intermediate_link_is_rejected_even_if_target_stays_in_allowed_tree(tmp_path):
    repo = tmp_path / "repo"
    _source(repo, "src/hive/real/note.py", "ALLOWED_MARKER\n")
    alias = repo / "src/hive/alias"
    _directory_link(alias, repo / "src/hive/real")
    index = CodeIndex(repo)

    assert index._load("src/hive/alias/note.py", alias / "note.py") is None
    assert [(hit["file"], hit["line"]) for hit in index.search_text("ALLOWED_MARKER")] == [
        ("src/hive/real/note.py", 1),
    ]


def test_warmed_cache_rejects_path_after_intermediate_link_swap(tmp_path):
    repo = tmp_path / "repo"
    _source(repo, "src/hive/bridge/note.py", "SAFE_MARKER\n")
    _source(repo, "docs/note.py", "SECRET_INSIDE_REPO\n")
    bridge = repo / "src/hive/bridge"
    index = CodeIndex(repo)
    assert index.search_text("SAFE_MARKER")

    _replace_with_link(bridge, repo / "docs", tmp_path)

    assert index.search_text("SAFE_MARKER") == []
    assert index.search_text("SECRET_INSIDE_REPO") == []


def test_force_refresh_rejects_link_swapped_after_manifest_walk(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _source(repo, "src/hive/bridge/note.py", "SAFE_MARKER\n")
    _source(repo, "docs/note.py", "SECRET_INSIDE_REPO\n")
    bridge = repo / "src/hive/bridge"
    index = CodeIndex(repo)
    assert index.search_text("SAFE_MARKER")
    original_paths = index._paths

    def raced_paths():
        paths = original_paths()
        _replace_with_link(bridge, repo / "docs", tmp_path)
        return paths

    monkeypatch.setattr(index, "_paths", raced_paths)
    index.refresh(force=True)

    assert index.search_text("SECRET_INSIDE_REPO") == []


def test_hardlink_to_outside_file_is_not_indexed(tmp_path):
    repo = tmp_path / "repo"
    outside = _source(tmp_path, "outside.py", "SECRET_OUTSIDE\n")
    inside = repo / "tests/link.py"
    inside.parent.mkdir(parents=True)
    try:
        os.link(outside, inside)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    assert outside.stat().st_nlink >= 2
    assert CodeIndex(repo).search_text("SECRET_OUTSIDE") == []


def test_directory_enumeration_stops_at_global_entry_budget(tmp_path, monkeypatch):
    for number in range(30):
        _source(tmp_path, f"src/hive/file_{number:02}.py", "needle\n")
    monkeypatch.setattr(code_search, "_MAX_WALK_ENTRIES", 5)
    real_scandir = os.scandir
    yielded = 0

    class CountedScanner:
        def __init__(self, path):
            self._scanner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self._scanner.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            item = next(self._scanner)
            yielded += 1
            return item

    monkeypatch.setattr(code_search.os, "scandir", CountedScanner)
    hits = CodeIndex(tmp_path).search_text("needle")

    assert len(hits) <= 5
    assert yielded <= 5


def test_cache_has_global_memory_budget_without_losing_search_results(tmp_path, monkeypatch):
    for number in range(3):
        _source(tmp_path, f"src/hive/file_{number}.py", f"needle_{number} = 1\n" + "x" * 100 + "\n")
    monkeypatch.setattr(code_search, "_MAX_CACHE_BYTES", 2_000)
    index = CodeIndex(tmp_path)

    assert len(index.search_text("needle_")) == 3
    assert index._cache_bytes <= 2_000
    assert len(index._cache) < 3
    assert len(index.search_text("needle_")) == 3
    assert index._cache_bytes <= 2_000


def test_periodic_content_verification_reads_only_one_bounded_batch(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for number in range(100):
        _source(tmp_path, f"src/hive/file_{number:03}.py", f"needle_{number} = 1\n")
    index = CodeIndex(tmp_path)
    assert len(index.search_text("needle_", limit=100)) == 100
    real_open = os.open
    reads = 0

    def counted_open(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(code_search.os, "open", counted_open)
    clock[0] = 10.0
    index.refresh()

    assert 0 < reads <= 64


def test_file_larger_than_cache_budget_is_searched_but_not_retained(tmp_path, monkeypatch):
    _source(tmp_path, "src/hive/large.py", "needle = 1\n" + "x" * 1000 + "\n")
    monkeypatch.setattr(code_search, "_MAX_CACHE_BYTES", 256)
    index = CodeIndex(tmp_path)

    assert [(hit["file"], hit["line"]) for hit in index.search_text("needle")] == [
        ("src/hive/large.py", 1),
    ]
    assert index._cache_bytes == 0
    assert not index._cache


def test_real_repository_definition_is_found():
    root = Path(__file__).resolve().parents[1]
    hits = CodeIndex(root).search_symbol("CodeIndex")

    assert any(
        hit["file"] == "src/hive/tools/code_search.py"
        and hit["kind"] == "definition"
        and hit["line"] > 0
        for hit in hits
    )
