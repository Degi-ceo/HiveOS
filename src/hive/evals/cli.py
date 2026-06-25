"""
cli.py — `hive eval` command-line interface.

Subcommands:
  run   <paths...>            Load datasets, run them against a target, emit
                              reports, exit non-zero on any failure.
  show  <report-file>         Pretty-print a previously-saved JUnit XML or
                              HTML report (re-uses the registered reporters).

`run` discovers its target by name. Two built-ins:
  --target hive       Use HiveOS.ask() — full agent round-trip.
  --target mock       A deterministic mock that returns the dataset's
                      `expected` field (great for CI gate testing without
                      burning real LLM tokens).

User-supplied targets are loaded via `--target <dotted.module:function>`
so an external project can plug in their own agent without touching Hive.

Exits:
  0 — all evals passed
  1 — at least one eval failed (CI gate behaviour)
  2 — malformed dataset or unrecoverable runner error
  3 — usage error (unknown args, missing target, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Sequence

from hive.evals.dataset import DatasetError, load_many
from hive.evals.reporters import get_reporter
from hive.evals.runner import make_report, run_async
from hive.evals.types import EvalItem

# ---------------------------------------------------------------------------
# Target loading
# ---------------------------------------------------------------------------

def _load_target(spec: str):
    """Resolve a `--target` value into a callable (item -> str / awaitable).

    Special values:
      * "hive"  — `HiveOS.ask()` (async). The real agent.
      * "mock"  — deterministic: returns `item.expected`. For CI gates.
      * "<dotted.module:callable>" — user-supplied target (sync or async).
    """
    if spec == "hive":
        return _hive_target()
    if spec == "mock":
        return _mock_target
    if ":" in spec:
        module_name, attr = spec.split(":", 1)
        mod = importlib.import_module(module_name)
        target = getattr(mod, attr)
        if not callable(target):
            raise ValueError(f"target {spec!r} resolved to non-callable {target!r}")
        return target
    raise ValueError(
        f"unknown target {spec!r}; expected 'hive', 'mock', or "
        "'module:callable'"
    )


def _hive_target():
    """Build a target that delegates to HiveOS.ask(). Imported lazily so the
    evals module can be used without pulling in the full HiveOS runtime
    (matters for `hive eval show` which never invokes the target)."""
    from hive import HiveOS

    async def ask_target(item: EvalItem) -> str:
        hive = HiveOS()
        return await hive.ask(item.input)

    return ask_target


def _mock_target(item: EvalItem) -> str:
    """Deterministic stand-in: returns the dataset's expected output verbatim.
    Lets CI exercise the eval pipeline end-to-end without API keys."""
    return item.expected


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

async def _cmd_run(args: argparse.Namespace) -> int:
    """`hive eval run` — load dataset(s), run, emit reports, set exit code."""
    try:
        target = _load_target(args.target)
    except (ValueError, ImportError, AttributeError) as e:
        print(f"hive eval: failed to load target: {e}", file=sys.stderr)
        return 3
    try:
        items = load_many(args.dataset)
    except DatasetError as e:
        print(f"hive eval: {e}", file=sys.stderr)
        return 2
    if not items:
        print("hive eval: dataset is empty — nothing to run", file=sys.stderr)
        return 2

    started_at = _now_iso()
    progress = _make_progress(args.quiet)
    try:
        results = await run_async(
            items,
            target,
            concurrency=args.concurrency,
            per_item_timeout=args.timeout,
            progress=progress,
        )
    except Exception as e:  # noqa: BLE001 — CLI must surface any runner failure
        print(f"hive eval: runner crashed: {e}", file=sys.stderr)
        return 2
    report = make_report(
        items, results,
        dataset_path=",".join(str(p) for p in args.dataset),
        started_at=started_at,
    )

    # Console reporter — always emitted (unless --quiet).
    if not args.quiet:
        try:
            get_reporter("console").write(report, sys.stdout)
        except KeyError as e:
            print(f"hive eval: {e}", file=sys.stderr)
            return 2

    # File reporters — one file per --report flag.
    for spec in args.report:
        try:
            fmt, _, path = spec.partition("=")
            if not path:
                print(
                    f"hive eval: --report {spec!r} must be 'format=path'",
                    file=sys.stderr,
                )
                return 3
            stream = open(path, "w", encoding="utf-8")
            with stream as s:
                get_reporter(fmt).write(report, s)
            print(f"hive eval: wrote {fmt} report to {path}", file=sys.stderr)
        except KeyError as e:
            print(f"hive eval: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"hive eval: could not write report: {e}", file=sys.stderr)
            return 2

    return 0 if report.summary.all_passed else 1


def _cmd_show(args: argparse.Namespace) -> int:
    """`hive eval show <report>` — re-render a saved report to stdout.

    Detects format by suffix (.xml -> junit_xml, .html -> html, anything
    else -> console summary derived from the file's JSON sidecar)."""
    path = Path(args.report_file)
    if not path.exists():
        print(f"hive eval: {path}: no such file", file=sys.stderr)
        return 2
    suffix = path.suffix.lower()
    if suffix == ".xml":
        with path.open("r", encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    if suffix in {".html", ".htm"}:
        with path.open("r", encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    if suffix == ".json":
        try:
            with path.open("r", encoding="utf-8") as f:
                report_dict = json.load(f)
        except json.JSONDecodeError as e:
            print(f"hive eval: {path}: invalid JSON: {e}", file=sys.stderr)
            return 2
        # Build a minimal console summary from the JSON sidecar — we don't
        # reconstruct full EvalResult objects (that'd require serialising
        # dataclasses properly); just print what we have.
        s = report_dict.get("summary", {})
        print(f"dataset: {report_dict.get('dataset_path')}")
        print(f"started: {report_dict.get('started_at')}")
        print(f"finished: {report_dict.get('finished_at')}")
        print(f"total: {s.get('total')}  passed: {s.get('passed')}  "
              f"failed: {s.get('failed')}  errored: {s.get('errored')}")
        print(f"pass rate: {s.get('pass_rate', 0) * 100:.1f}%")
        return 0 if s.get("all_passed") else 1
    print(
        f"hive eval: cannot infer report format from suffix {suffix!r}; "
        f"use .xml, .html, or .json",
        file=sys.stderr,
    )
    return 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_progress(quiet: bool):
    """Return a progress callback for the runner. When `quiet` is True, this
    returns None so the runner skips progress invocation entirely."""
    if quiet:
        return None

    def _on_result(r) -> None:
        marker = "PASS" if r.passed else ("ERR " if r.error else "FAIL")
        print(f"  [{marker}] {r.item.id}", file=sys.stderr)

    return _on_result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hive eval",
        description="HiveOS eval harness — run regression suites and emit reports.",
    )
    # sub is NOT required: `main()` wants to handle the no-command case
    # itself (prints help + returns exit code 3 per the cli.py docstring),
    # instead of letting argparse call sys.exit(2) behind our backs.
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Load a dataset and run it against a target")
    p_run.add_argument("dataset", nargs="+", type=Path, help="Path(s) to .jsonl or .yaml dataset")
    p_run.add_argument(
        "--target", default="mock",
        help="Target spec: 'hive', 'mock', or 'module:callable' (default: mock)",
    )
    p_run.add_argument(
        "--concurrency", type=int, default=4,
        help="Max concurrent evals (default: 4)",
    )
    p_run.add_argument(
        "--timeout", type=float, default=30.0,
        help="Per-item timeout in seconds (default: 30)",
    )
    p_run.add_argument(
        "--report", action="append", default=[],
        help="Emit a report file: 'format=path' (repeatable; e.g. junit_xml=out.xml)",
    )
    p_run.add_argument(
        "--quiet", action="store_true",
        help="Suppress console output (only emit --report files + exit code)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_show = sub.add_parser("show", help="Pretty-print a previously-saved report")
    p_show.add_argument("report_file", type=Path, help="Path to a saved report")
    p_show.set_defaults(func=_cmd_show)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `hive eval` command (and the standalone
    `hive-eval` script). Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 3
    if inspect.iscoroutinefunction(func):
        return asyncio.run(func(args))
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
