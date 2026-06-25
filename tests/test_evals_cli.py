"""Tests for hive.evals.cli — argparse, target loading, run + show subcommands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from hive.evals.cli import (
    _cmd_run,
    _cmd_show,
    _dynamic_target_enabled,
    _load_target,
    _mock_target,
    _validate_report_path,
    build_parser,
    main,
)
from hive.evals.types import EvalItem


# ---------- build_parser -------------------------------------------------------

def test_build_parser_has_run_and_show():
    parser = build_parser()
    # Parse a no-op invocation to confirm shape
    args = parser.parse_args(["run", "ds.jsonl"])
    assert args.command == "run" and args.dataset == [Path("ds.jsonl")]


def test_build_parser_run_defaults():
    parser = build_parser()
    args = parser.parse_args(["run", "ds.jsonl"])
    assert args.target == "mock"
    assert args.concurrency == 4
    assert args.timeout == 30.0
    assert args.quiet is False
    assert args.report == []


def test_build_parser_run_accepts_multiple_datasets():
    parser = build_parser()
    args = parser.parse_args(["run", "a.jsonl", "b.jsonl"])
    assert args.dataset == [Path("a.jsonl"), Path("b.jsonl")]


# ---------- _load_target ------------------------------------------------------

def test_load_target_mock():
    target = _load_target("mock")
    assert callable(target)
    item = EvalItem(id="t", input="in", expected="hi", grader="exact")
    assert target(item) == "hi"


def test_load_target_module_callable(tmp_path):
    # Write a tiny module to a temp dir and add to sys.path
    mod_path = tmp_path / "tmptarget.py"
    mod_path.write_text("def target(item):\n    return 'from-mod'\n")
    sys.path.insert(0, str(tmp_path))
    try:
        loaded = _load_target(f"{mod_path.stem}:target", allow_dynamic=True)
        item = EvalItem(id="t", input="in", expected="x", grader="exact")
        assert loaded(item) == "from-mod"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("tmptarget", None)


def test_load_target_module_callable_blocked_without_opt_in(tmp_path):
    """Security gate: importing an arbitrary module from `--target` is OFF
    by default. A dataset author cannot pivot to RCE by changing the target
    spec unless the operator has explicitly opted in."""
    mod_path = tmp_path / "tmptarget.py"
    mod_path.write_text("def target(item):\n    return 'from-mod'\n")
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(ValueError) as exc:
            _load_target(f"{mod_path.stem}:target")
        assert "disabled by default" in str(exc.value)
        assert "--allow-dynamic-target" in str(exc.value)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("tmptarget", None)


def test_load_target_module_non_callable_raises(tmp_path):
    mod_path = tmp_path / "tmptarget2.py"
    mod_path.write_text("NOT_CALLABLE = 42\n")
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(ValueError) as exc:
            _load_target("tmptarget2:NOT_CALLABLE", allow_dynamic=True)
        assert "non-callable" in str(exc.value)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("tmptarget2", None)


def test_load_target_unknown_spec_raises():
    with pytest.raises(ValueError) as exc:
        _load_target("totally-unknown")
    assert "unknown target" in str(exc.value)


def test_load_target_hive_returns_callable():
    """The 'hive' target loads lazily — calling it should not crash even
    without a real HiveOS config because we only import the module on call."""
    target = _load_target("hive")
    assert callable(target)


def test_hive_target_actually_invokes_hive(monkeypatch):
    """The 'hive' target should construct a HiveOS and call ask() on it.
    We monkeypatch the HiveOS symbol in `hive` so the test doesn't touch
    real LLM APIs (the lazy `from hive import HiveOS` inside `_hive_target`
    re-reads the attribute at call time)."""
    import hive as hive_pkg

    class FakeHiveOS:
        instances: list["FakeHiveOS"] = []

        def __init__(self):
            self.calls: list[str] = []
            FakeHiveOS.instances.append(self)

        async def ask(self, prompt: str) -> str:
            self.calls.append(prompt)
            return f"echo:{prompt}"

    monkeypatch.setattr(hive_pkg, "HiveOS", FakeHiveOS)
    target = _load_target("hive")
    item = EvalItem(id="t", input="hi", expected="x", grader="exact")
    import asyncio
    # Pyright can't narrow `Target = Awaitable[str] | str` to a Coroutine for
    # asyncio.run; runtime is correct because 'hive' is always async.
    result = target(item)
    if asyncio.iscoroutine(result):
        out = asyncio.run(result)
    else:
        out = result
    assert out == "echo:hi"
    assert len(FakeHiveOS.instances) == 1
    assert FakeHiveOS.instances[0].calls == ["hi"]


def test_load_target_import_error_caught_by_caller():
    """If the dotted module doesn't exist, importlib raises ImportError.
    `_cmd_run` catches (ValueError, ImportError, AttributeError). Requires
    explicit allow_dynamic=True — the opt-in gate runs before importlib."""
    with pytest.raises(ImportError):
        _load_target("nonexistent.module.path:fn", allow_dynamic=True)


def test_load_target_import_error_blocked_before_import():
    """Even an obviously-bogus module spec must be refused by the opt-in
    gate so the user sees the helpful message rather than a stack trace."""
    with pytest.raises(ValueError) as exc:
        _load_target("nonexistent.module.path:fn")
    assert "disabled by default" in str(exc.value)


# ---------- _dynamic_target_enabled -------------------------------------------

def test_dynamic_target_disabled_by_default(monkeypatch):
    """No CLI flag, no env var → refuse dynamic targets."""
    monkeypatch.delenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", raising=False)
    assert _dynamic_target_enabled(None) is False
    args = argparse.Namespace(allow_dynamic_target=False)
    assert _dynamic_target_enabled(args) is False


def test_dynamic_target_enabled_via_cli_flag(monkeypatch):
    """`--allow-dynamic-target` flips the decision regardless of env."""
    monkeypatch.delenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", raising=False)
    args = argparse.Namespace(allow_dynamic_target=True)
    assert _dynamic_target_enabled(args) is True


def test_dynamic_target_enabled_via_env_var(monkeypatch):
    """Truthy env values flip the decision without the CLI flag."""
    monkeypatch.delenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", raising=False)
    args = argparse.Namespace(allow_dynamic_target=False)
    for truthy in ("1", "true", "yes", "on", "TRUE", "YES"):
        monkeypatch.setenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", truthy)
        assert _dynamic_target_enabled(args) is True, truthy


def test_dynamic_target_env_garbage_is_falsy(monkeypatch):
    """Unrecognised env values are NOT treated as opt-in — safer default."""
    monkeypatch.delenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", raising=False)
    args = argparse.Namespace(allow_dynamic_target=False)
    for falsy in ("0", "no", "off", "", "garbage", " "):
        monkeypatch.setenv("HIVE_EVAL_ALLOW_DYNAMIC_TARGET", falsy)
        assert _dynamic_target_enabled(args) is False, falsy


# ---------- _validate_report_path ---------------------------------------------

def test_validate_report_path_accepts_relative():
    p = _validate_report_path("out/report.xml")
    assert p == Path("out/report.xml")


def test_validate_report_path_rejects_absolute():
    with pytest.raises(ValueError) as exc:
        _validate_report_path("/etc/passwd")
    assert "must be relative" in str(exc.value)


def test_validate_report_path_rejects_home_expansion():
    with pytest.raises(ValueError) as exc:
        _validate_report_path("~/report.xml")
    assert "~" in str(exc.value)


def test_validate_report_path_rejects_dotdot():
    """`a/../../b.xml` collapses to `../b.xml` which still has a `..` segment —
    rejected. (By contrast `a/../b.xml` collapses fully to `b.xml` and is
    accepted — that's safe, the `..` is consumed by an earlier component.)"""
    with pytest.raises(ValueError) as exc:
        _validate_report_path("a/../../b.xml")
    assert ".." in str(exc.value)


def test_validate_report_path_rejects_leading_dotdot():
    with pytest.raises(ValueError):
        _validate_report_path("../escape.xml")


def test_validate_report_path_rejects_empty():
    with pytest.raises(ValueError) as exc:
        _validate_report_path("")
    assert "empty" in str(exc.value)


def test_cmd_run_report_absolute_path_exits_3(tmp_path, capsys):
    """`--report /tmp/foo.xml` must be refused with exit code 3 (usage)."""
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "junit_xml=/etc/evil.xml", "--quiet"])
    assert rc == 3
    assert "must be relative" in capsys.readouterr().err


def test_cmd_run_report_dotdot_path_exits_3(tmp_path, capsys):
    """`--report ../escape.xml` must be refused with exit code 3."""
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "html=../escape.html", "--quiet"])
    assert rc == 3
    assert ".." in capsys.readouterr().err


# ---------- _mock_target ------------------------------------------------------

def test_mock_target_returns_expected():
    item = EvalItem(id="t", input="in", expected="expected-text", grader="exact")
    assert _mock_target(item) == "expected-text"


# ---------- _cmd_run -----------------------------------------------------------

async def _run(parser_args) -> int:
    args = build_parser().parse_args(parser_args)
    return await _cmd_run(args)


def _write_dataset(tmp_path: Path, name: str = "ds.jsonl", **item) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({
        "id": item.get("id", "x"),
        "input": item.get("input", "in"),
        "expected": item.get("expected", "out"),
        "grader": item.get("grader", "exact"),
        **({"extra": item["extra"]} if "extra" in item else {}),
    }) + "\n")
    return p


def test_cmd_run_passing_dataset_exits_0(tmp_path, capsys):
    p = _write_dataset(tmp_path, expected="hello", input="x")
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALL PASSED" in out


def test_cmd_run_failing_dataset_exits_1(tmp_path, capsys, monkeypatch):
    """A failing eval must exit 1. The built-in `mock` target returns
    `item.expected`, which by definition matches the grader — so to test the
    failure path we use a custom module target that returns a fixed wrong
    string. The opt-in flag is required to load a dynamic target."""
    mod_path = tmp_path / "always_wrong.py"
    mod_path.write_text("def target(item):\n    return 'WRONG'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    p = tmp_path / "ds.jsonl"
    p.write_text(json.dumps({
        "id": "x", "input": "in", "expected": "CORRECT", "grader": "exact",
    }) + "\n")
    rc = asyncio_run(_run, ["run", str(p),
                            "--target", "always_wrong:target",
                            "--allow-dynamic-target"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "EVALS FAILED" in out


def test_cmd_run_dataset_error_exits_2(tmp_path, capsys):
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n")
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "DatasetError" in err or "invalid JSON" in err


def test_cmd_run_empty_dataset_exits_2(tmp_path, capsys):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock"])
    assert rc == 2
    assert "empty" in capsys.readouterr().err


def test_cmd_run_unknown_target_exits_3(tmp_path, capsys):
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "no-such"])
    assert rc == 3
    assert "failed to load target" in capsys.readouterr().err


def test_cmd_run_quiet_suppresses_console(capsys, tmp_path):
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock", "--quiet"])
    assert rc == 0
    assert "ALL PASSED" not in capsys.readouterr().out


def test_cmd_run_writes_junit_xml_report(tmp_path, monkeypatch):
    p = _write_dataset(tmp_path)
    out_xml = tmp_path / "report.xml"
    # chdir so the relative report-path validator accepts our local filename.
    monkeypatch.chdir(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "junit_xml=report.xml", "--quiet"])
    assert rc == 0
    assert out_xml.exists()
    content = out_xml.read_text()
    assert "<testsuite" in content
    assert content.startswith("<?xml")


def test_cmd_run_writes_html_report(tmp_path, monkeypatch):
    p = _write_dataset(tmp_path)
    out_html = tmp_path / "report.html"
    monkeypatch.chdir(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "html=report.html", "--quiet"])
    assert rc == 0
    assert out_html.exists()
    assert out_html.read_text().startswith("<!doctype html>")


def test_cmd_run_writes_console_report(tmp_path, monkeypatch):
    p = _write_dataset(tmp_path)
    out_txt = tmp_path / "report.txt"
    monkeypatch.chdir(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "console=report.txt", "--quiet"])
    assert rc == 0
    assert "ALL PASSED" in out_txt.read_text()


def test_cmd_run_report_without_equals_exits_3(tmp_path, capsys):
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "junit_xml", "--quiet"])
    assert rc == 3
    assert "'format=path'" in capsys.readouterr().err


def test_cmd_run_report_unknown_format_exits_2(tmp_path, capsys):
    p = _write_dataset(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "no_such_fmt=out", "--quiet"])
    assert rc == 2
    assert "unknown reporter" in capsys.readouterr().err


def test_cmd_run_report_os_error_exits_2(tmp_path, capsys, monkeypatch):
    """Writing to a path inside a non-existent directory triggers OSError."""
    p = _write_dataset(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock",
                            "--report", "junit_xml=no-such-dir/x.xml", "--quiet"])
    assert rc == 2
    assert "could not write" in capsys.readouterr().err


def test_cmd_run_console_reporter_unknown_exits_2(tmp_path, capsys):
    """If the console reporter is removed from the registry at runtime, exit 2."""
    from hive.evals import reporters as rep_mod
    p = _write_dataset(tmp_path)
    rep_mod.REPORTERS.pop("console", None)
    try:
        rc = asyncio_run(_run, ["run", str(p), "--target", "mock"])
        assert rc == 2
        assert "unknown reporter" in capsys.readouterr().err
    finally:
        from hive.evals.reporters.console import ConsoleReporter
        rep_mod.register_reporter(ConsoleReporter())


def test_cmd_run_runner_crash_exits_2(tmp_path, capsys, monkeypatch):
    """If the runner itself raises (not a per-item error, but a true crash),
    the CLI must surface exit code 2 with a clear stderr message."""
    from hive.evals import cli as cli_inner
    p = _write_dataset(tmp_path)

    async def boom(items, target, **_kw):
        raise RuntimeError("runner exploded")
    monkeypatch.setattr(cli_inner, "run_async", boom)
    rc = asyncio_run(_run, ["run", str(p), "--target", "mock", "--quiet"])
    assert rc == 2
    assert "runner crashed" in capsys.readouterr().err


# ---------- _cmd_show ---------------------------------------------------------

def _write_report_json(tmp_path, *, all_passed: bool, total: int = 1) -> Path:
    p = tmp_path / "rep.json"
    p.write_text(json.dumps({
        "dataset_path": "x.jsonl",
        "started_at": "s",
        "finished_at": "f",
        "results": [],
        "summary": {
            "total": total, "passed": total if all_passed else 0,
            "failed": 0 if all_passed else total, "errored": 0,
            "avg_score": 1.0, "total_duration_ms": 0.0,
            "pass_rate": 1.0 if all_passed else 0.0,
            "all_passed": all_passed,
        },
    }))
    return p


def test_cmd_show_missing_file_exits_2(tmp_path, capsys):
    p = tmp_path / "nope.json"
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 2
    assert "no such file" in capsys.readouterr().err


def test_cmd_show_xml_outputs_file_contents(tmp_path, capsys):
    p = tmp_path / "r.xml"
    p.write_text("<xml>hi</xml>")
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 0
    assert capsys.readouterr().out == "<xml>hi</xml>"


def test_cmd_show_html_outputs_file_contents(tmp_path, capsys):
    p = tmp_path / "r.html"
    p.write_text("<html>hi</html>")
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 0
    assert capsys.readouterr().out == "<html>hi</html>"


def test_cmd_show_htm_suffix(tmp_path, capsys):
    p = tmp_path / "r.htm"
    p.write_text("<html>x</html>")
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 0


def test_cmd_show_json_all_passed_exits_0(tmp_path, capsys):
    p = _write_report_json(tmp_path, all_passed=True)
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALL" in out.upper() or "total" in out


def test_cmd_show_json_failed_exits_1(tmp_path, capsys):
    p = _write_report_json(tmp_path, all_passed=False)
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 1


def test_cmd_show_json_invalid_exits_2(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_cmd_show_unknown_suffix_exits_3(tmp_path, capsys):
    p = tmp_path / "x.txt"
    p.write_text("hi")
    args = build_parser().parse_args(["show", str(p)])
    rc = _cmd_show(args)
    assert rc == 3


# ---------- main entrypoint --------------------------------------------------

def test_main_no_args_exits_3(capsys):
    rc = main([])
    assert rc == 3
    assert "usage" in capsys.readouterr().err.lower() or "eval" in capsys.readouterr().err.lower()


def test_main_routes_to_sync_subcommand(tmp_path, monkeypatch, capsys):
    """`show` is sync — main() invokes it without asyncio.run."""
    p = _write_report_json(tmp_path, all_passed=True)
    rc = main(["show", str(p)])
    assert rc == 0


def test_main_routes_to_async_subcommand(tmp_path):
    p = _write_dataset(tmp_path, expected="hello")
    rc = main(["run", str(p), "--target", "mock", "--quiet"])
    assert rc == 0


def test_cli_module_main_block_executes(tmp_path):
    """Cover the `if __name__ == "__main__":` line by executing the module
    as a script. Args get passed via sys.argv; we exit with a known code so
    runpy returns it. Uses `--help` which is always available and exits 0."""
    import runpy
    import sys
    rc = runpy.run_module("hive.evals.cli", run_name="__notmain__")
    assert callable(rc["main"])
    # And actually exercise the __main__ branch by running with --help:
    backup = sys.argv
    try:
        sys.argv = ["hive-eval", "--help"]
        try:
            runpy.run_module("hive.evals.cli", run_name="__main__")
        except SystemExit as e:
            assert e.code == 0
    finally:
        sys.argv = backup


# ---------- helper ------------------------------------------------------------

def asyncio_run(coro_fn, args):
    """Sync helper to drive an async function from pytest tests."""
    import asyncio
    return asyncio.run(coro_fn(args))
