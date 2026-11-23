"""Check value-free HTTP contract diagnostics with synthetic reports."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.contracts import ci_diagnostics as diagnostics


class _Report:
    failed = True
    when = "call"
    location = ("tests/contracts/test_http_schema.py", 120, "unused")

    def __getattr__(self, name):
        raise AssertionError("Failure payload access is forbidden")


class Diagnostics(unittest.TestCase):
    def setUp(self) -> None:
        diagnostics._SEEN.clear()

    def emit(self, report, collect=False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            if collect:
                diagnostics.pytest_collectreport(report)
            else:
                diagnostics.pytest_runtest_logreport(report)
        return output.getvalue()

    def check_xml(self, payload: bytes | None) -> tuple[bool, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.xml"
            summary = Path(directory) / "summary"
            if payload is not None:
                report.write_bytes(payload)
            output = io.StringIO()
            with (
                redirect_stdout(output),
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
            ):
                accepted = diagnostics.verify_report(report)
            return accepted, output.getvalue(), summary.read_text() if summary.exists() else ""

    def test_failure_reports_only_source_location_and_static_phase(self) -> None:
        self.assertEqual(
            self.emit(_Report()),
            "::error file=tests/contracts/test_http_schema.py,line=121::HTTP contract failed during call.\n",
        )
        self.assertEqual(self.emit(_Report()), "")

    def test_other_paths_and_successes_emit_nothing(self) -> None:
        for path in ("/private/test.py", "../test.py", "tests/contracts/test_http_schema.py%0A"):
            self.assertEqual(
                self.emit(SimpleNamespace(failed=True, location=(path, 0), when="call")), ""
            )
        self.assertEqual(self.emit(SimpleNamespace(failed=False)), "")

    def test_bad_lines_are_omitted_and_phases_are_static(self) -> None:
        for line in (True, -1, 1_000_000, "private"):
            diagnostics._SEEN.clear()
            output = self.emit(
                SimpleNamespace(
                    failed=True, location=(_Report.location[0], line), when="injected%0A"
                )
            )
            self.assertEqual(
                output,
                "::error file=tests/contracts/test_http_schema.py::HTTP contract failed during unknown.\n",
            )

    def test_annotations_are_bounded_and_collection_needs_no_payload(self) -> None:
        for line in range(10):
            self.assertTrue(
                self.emit(
                    SimpleNamespace(failed=True, location=(_Report.location[0], line), when="call")
                )
            )
        self.assertEqual(
            self.emit(
                SimpleNamespace(failed=True, location=(_Report.location[0], 20), when="call")
            ),
            "",
        )
        diagnostics._SEEN.clear()
        self.assertEqual(
            self.emit(SimpleNamespace(failed=True), collect=True),
            "::error::HTTP contract collection failed.\n",
        )
        self.assertEqual(self.emit(SimpleNamespace(failed=True), collect=True), "")

    def test_exact_execution_counts_accept_without_reading_failure_text(self) -> None:
        xml = b'<testsuites><testsuite tests="10" skipped="0" failures="0" errors="0"><testcase name="unread"><system-out>PRIVATE_SENTINEL</system-out></testcase></testsuite></testsuites>'
        accepted, output, summary = self.check_xml(xml)
        self.assertTrue(accepted)
        self.assertIn('"tests": 10', output)
        self.assertIn("HTTP contract: 10 tests", summary)
        self.assertNotIn("PRIVATE_SENTINEL", output + summary)
        self.assertNotIn("unread", output + summary)

    def test_missing_malformed_oversized_and_invalid_counts_refuse(self) -> None:
        for payload in (
            None,
            b"<bad",
            b"x" * (1024 * 1024 + 1),
            b'<testsuites><testsuite tests="-1"/></testsuites>',
            b'<testsuites><testsuite tests="PRIVATE_SENTINEL"/></testsuites>',
        ):
            accepted, output, summary = self.check_xml(payload)
            self.assertFalse(accepted)
            self.assertTrue(output.startswith("::error::"))
            self.assertEqual(summary, "")
            self.assertNotIn("PRIVATE_SENTINEL", output)

    def test_wrong_counts_refuse_without_dumping_testcases(self) -> None:
        for field, value in (("tests", 9), ("skipped", 1), ("failures", 1), ("errors", 1)):
            counts = {"tests": 10, "skipped": 0, "failures": 0, "errors": 0}
            counts[field] = value
            attributes = " ".join(f'{key}="{count}"' for key, count in counts.items())
            accepted, output, summary = self.check_xml(
                f'<testsuites><testsuite {attributes}><testcase name="PRIVATE_SENTINEL"/></testsuite></testsuites>'.encode()
            )
            self.assertFalse(accepted)
            self.assertIn("requires ten tests", output)
            self.assertNotIn("PRIVATE_SENTINEL", output)
            self.assertEqual(summary, "")


if __name__ == "__main__":
    unittest.main()
