"""
junit_xml.py — JUnit-XML reporter (consumed by GitHub Actions annotations,
Jenkins, GitLab, etc.). Stdlib only (`xml.etree.ElementTree`) — no extra deps.

Format reference: Ant JUnit schema (the de-facto standard). One `<testsuite>`
per report, one `<testcase>` per EvalResult, with a `<failure>` / `<error>`
child element when the case did not pass. `classname` is the grader name —
that gives CI dashboards a useful breakdown (e.g. "3 failures in regex").

SECURITY NOTE: this module only GENERATES XML — it never parses untrusted
input. We deliberately do NOT use `minidom.parseString` for pretty-printing
because the stdlib XML parsers are vulnerable to XXE / billion-laughs attacks
on untrusted input (per security guidance). The output here is unindented
single-line XML, which every CI consumer (Jenkins, GitHub Actions, GitLab,
pytest-junit) accepts without complaint. If you ever need pretty-printed XML,
prefer `defusedxml.ElementTree` and never accept raw XML from the network.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import IO, TextIO

from hive.evals.types import EvalReport

_NAME = "junit_xml"


def render(report: EvalReport, stream: TextIO) -> None:
    """Render the report as a JUnit XML document and write to `stream`.
    Output is single-line (no pretty-print) — see SECURITY NOTE above for why
    we avoid minidom and defusedxml wasn't pulled in just for cosmetic indent."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        attrib={
            "name": report.dataset_path,
            "tests": str(report.summary.total),
            "failures": str(report.summary.failed),
            "errors": str(report.summary.errored),
            "time": f"{report.summary.total_duration_ms / 1000:.3f}",
            "timestamp": report.started_at,
        },
    )
    for r in report.results:
        case = ET.SubElement(
            suite,
            "testcase",
            attrib={
                "classname": r.item.grader,
                "name": r.item.id,
                "time": f"{r.duration_ms / 1000:.3f}",
            },
        )
        if r.error is not None:
            err = ET.SubElement(case, "error", attrib={"message": "target raised", "type": "RuntimeError"})
            err.text = r.error
        elif not r.grader_result.passed:
            fail = ET.SubElement(
                case, "failure",
                attrib={"message": r.grader_result.message or "grader rejected", "type": "AssertionError"},
            )
            fail.text = f"expected: {r.item.expected!r}\noutput:   {r.output!r}"
    stream.write('<?xml version="1.0" encoding="utf-8"?>\n')
    stream.write(ET.tostring(root, encoding="unicode"))


def make() -> "JUnitXMLReporter":
    return JUnitXMLReporter()


class JUnitXMLReporter:
    name = _NAME

    def write(self, report: EvalReport, stream: IO[str]) -> None:
        # `IO[str]` and TextIO overlap for our purposes; cast for type-check.
        render(report, stream)  # type: ignore[arg-type]
