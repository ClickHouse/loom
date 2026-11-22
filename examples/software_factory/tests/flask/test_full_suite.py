import tempfile
import unittest
from pathlib import Path

from examples.software_factory.evals.flask.full_suite import interpret, regressions


class FullSuite(unittest.TestCase):
    def test_regressions_exclude_existing_failures_and_include_lost_passing_checks(self):
        base = {"valid": True, "tests": {"ok": "PASSED", "issue": "FAILED", "lost": "PASSED"}}
        fixed = {"valid": True, "tests": {"ok": "PASSED", "issue": "PASSED", "lost": "PASSED"}}
        self.assertEqual(regressions(base, fixed), [])
        for status in ("FAILED", "SKIPPED"):
            candidate = {"valid": True, "tests": {"ok": status, "issue": "FAILED"}}
            self.assertEqual(regressions(base, candidate), ["lost", "ok"])

    def test_unmeasured_or_invalid_suite_is_not_zero_regressions(self):
        for candidate in (None, {"valid": False}, {"valid": True, "tests": {}}):
            self.assertIsNone(regressions({"valid": False}, candidate))

    def test_full_suite_report_rejects_missing_timeout_and_inconsistent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            xml = Path(temporary) / "suite.xml"
            self.assertFalse(interpret(xml, 0, False)["valid"])
            self.assertFalse(interpret(xml, 0, True)["valid"])
            xml.write_text(
                '<testsuite><testcase file="tests/test_app.py" '
                'classname="tests.test_app" name="test_ok"/></testsuite>'
            )
            self.assertTrue(interpret(xml, 0, False)["valid"])
            self.assertFalse(interpret(xml, 1, False)["valid"])
            xml.write_text(
                '<testsuite errors="1"><testcase file="tests/test_app.py" '
                'classname="tests.test_app" name="test_bad"><error/></testcase></testsuite>'
            )
            self.assertFalse(interpret(xml, 1, False)["valid"])
