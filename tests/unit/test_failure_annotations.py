"""Failure annotations expose declared locations and no captured test values."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests._failure_annotations import MAX_MODULES, emit_failed_module


class LocationOnlyReport:
    failed = True
    location = ("tests/unit/test_example.py", 12, "private-parameter")

    def __getattr__(self, name: str) -> object:
        raise AssertionError("Captured report values must not be inspected")


class FailureAnnotationsTest(unittest.TestCase):
    def emit(self, *, path="tests/unit/test_example.py", line=0, failed=True, seen=None):
        output = []
        report = SimpleNamespace(failed=failed, location=(path, line, "private-parameter"))
        emit_failed_module(report, set() if seen is None else seen, output.append, enabled=True)
        return output

    def test_declared_line_is_one_based(self):
        self.assertEqual(
            self.emit(line=12),
            [
                "::error file=tests/unit/test_example.py,line=13::"
                "A test declared in this module failed.\n"
            ],
        )

    def test_disabled_does_not_inspect_report(self):
        emit_failed_module(object(), set(), self.fail, enabled=False)

    def test_success_and_skip_do_not_emit(self):
        self.assertEqual(self.emit(failed=False), [])

    def test_only_declared_location_is_read(self):
        output = []
        emit_failed_module(LocationOnlyReport(), set(), output.append, enabled=True)
        self.assertEqual(len(output), 1)
        self.assertNotIn("private", output[0])

    def test_invalid_paths_are_rejected(self):
        for path in (
            "/tests/unit/test_a.py",
            "../tests/test_a.py",
            "tests/../test_a.py",
            "tests/./test_a.py",
            "tests//test_a.py",
            "tests\\test_a.py",
            "tests/test_a.py\n::error::private",
            "tests/test_a\r.py",
            "tests/test_a\x00.py",
            "tests/test_a:line=2.py",
            "tests/test_é.py",
            "tests/test_a.txt",
            "tests/" + "a" * 240 + ".py",
            None,
        ):
            with self.subTest(path=path):
                self.assertEqual(self.emit(path=path), [])

    def test_command_properties_are_escaped(self):
        self.assertEqual(
            self.emit(path="tests/unit/test_a,%0A.py")[0],
            "::error file=tests/unit/test_a%2C%250A.py,line=1::"
            "A test declared in this module failed.\n",
        )

    def test_invalid_declared_lines_are_omitted(self):
        for line in (None, True, False, -1, 1_000_000, "1\n::error::private", object()):
            with self.subTest(line=line):
                self.assertEqual(
                    self.emit(line=line),
                    [
                        "::error file=tests/unit/test_example.py::"
                        "A test declared in this module failed.\n"
                    ],
                )

    def test_only_first_failure_per_module_is_emitted(self):
        seen = set()
        self.assertEqual(len(self.emit(line=2, seen=seen)), 1)
        self.assertEqual(self.emit(line=8, seen=seen), [])
        self.assertEqual(len(self.emit(path="tests/unit/test_other.py", seen=seen)), 1)

    def test_annotations_have_a_session_budget(self):
        seen = {f"tests/unit/test_{index}.py" for index in range(MAX_MODULES)}
        self.assertEqual(self.emit(seen=seen), [])
        self.assertEqual(len(seen), MAX_MODULES)

    def test_output_failure_preserves_result_and_is_not_retried(self):
        for error in (OSError("private-value"), RuntimeError("private-value")):
            with self.subTest(error=type(error)):
                seen = set()
                calls = []

                def fail_write(annotation, calls=calls, error=error):
                    calls.append(annotation)
                    raise error

                report = LocationOnlyReport()
                emit_failed_module(report, seen, fail_write, enabled=True)
                emit_failed_module(report, seen, fail_write, enabled=True)
                self.assertTrue(report.failed)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("private", calls[0])

    def test_interrupts_are_not_swallowed(self):
        def interrupt(annotation):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            emit_failed_module(LocationOnlyReport(), set(), interrupt, enabled=True)
