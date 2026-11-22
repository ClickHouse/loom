from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from examples.software_factory.evals.flask import grading as harbor_grading
from examples.software_factory.factory.engine import Factory


class AuthoredRegressions(unittest.TestCase):
    def setUp(self):
        from examples.software_factory.tests.factory.test_factory import FactoryContracts

        self.fixture = FactoryContracts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_live_capability_and_public_requirement_instructions_are_equal_in_every_arm(self):
        prompts, schemas = [], []
        for mode in ("baseline", "natural"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                original = self.fixture.root
                self.fixture.root = Path(directory)
                try:
                    grader = self.fixture.factory(mode=mode).grader
                    grader.supports_regression_tests = True
                    from examples.software_factory.factory.adapters import (
                        ReplayContext,
                        ReplayProvider,
                    )
                    from examples.software_factory.tests.factory.test_factory import ROOT

                    factory = Factory(
                        self.fixture.task,
                        ReplayProvider(ROOT / self.fixture.frozen["replay"]["path"]),
                        grader,
                        Path(directory) / "live",
                        mode=mode,
                        backend=None if mode == "baseline" else ReplayContext(self.fixture.task),
                    )
                finally:
                    self.fixture.root = original
                prompts.append(factory.messages[0]["content"][0]["text"])
                schemas.append(next(t for t in factory.tools if t["name"] == "regression_test"))
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(schemas[0], schemas[1])
        self.assertIn("public task requirements", prompts[0])
        self.assertIn("regression_test", prompts[0])

    def test_offline_grader_does_not_expose_an_execution_capability(self):
        factory = self.fixture.factory(mode="baseline")
        self.assertNotIn("regression_test", {t["name"] for t in factory.tools})
        with self.assertRaises(PermissionError):
            factory.execute("regression_test", {"source": "def test_one(): assert True"})

    def test_regression_feedback_never_marks_fixed_or_hidden_verification_as_passed(self):
        from examples.software_factory.factory.adapters import ReplayProvider
        from examples.software_factory.tests.factory.test_factory import ROOT

        grader = self.fixture.factory(mode="baseline").grader
        grader.supports_regression_tests = True
        grader.regression = Mock(return_value={"passed": True, "state": "authored"})
        factory = Factory(
            self.fixture.task,
            ReplayProvider(ROOT / self.fixture.frozen["replay"]["path"]),
            grader,
            self.fixture.root / "live",
            mode="baseline",
        )
        before = factory.checkout.files()
        source = "def test_regression():\n    assert True\n"
        result = factory.execute("regression_test", {"source": source})
        self.assertTrue(result["passed"])
        self.assertFalse(result["task_completion_verified"])
        self.assertEqual(result["scope"], "agent_authored_regression")
        self.assertFalse(factory.visible_passed)
        self.assertEqual(factory.verified_state, "")
        self.assertEqual(factory.checkout.files(), before)
        grader.regression.assert_called_once_with(factory.checkout, source)

    def test_authored_source_is_bounded_and_parsed_without_execution(self):
        validate = harbor_grading.validate_regression_source
        self.assertEqual(
            validate("def test_one():\n    assert True\n"), "def test_one():\n    assert True\n"
        )
        for source in (None, "", "x" * 16001, "\x00", "def broken("):
            with self.subTest(source_type=type(source).__name__), self.assertRaises(ValueError):
                validate(source)
        with patch("builtins.exec", side_effect=AssertionError("Executed source")):
            validate("raise RuntimeError('must not execute while validating')\n")

    def test_failed_authored_assertions_return_bounded_detail_without_grading_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            fixed = workspace / "test_fixed.py"
            fixed.write_text("# evaluator-owned\n")
            authored = root / "authored.py"
            source = "def test_requirement():\n    assert 1 == 2\n"
            authored.write_text(source)
            captured = {}

            def sandbox_process(command, cwd, results):
                test_file = Path(command[-1])
                self.assertEqual(test_file.read_text(), source)
                self.assertFalse(test_file.is_relative_to(workspace))
                self.assertEqual(test_file.stat().st_mode & 0o222, 0)
                self.assertIn("-c", command)
                self.assertTrue(any(a.startswith("--confcutdir=") for a in command))
                self.assertTrue(any(a.startswith("--rootdir=") for a in command))
                self.assertEqual(cwd, workspace)
                (results / "stdout").write_text("x" * 8000 + "\nassert 1 == 2\n")
                (results / "stderr").write_text("")
                (results / "junit.xml").write_text(
                    '<testsuites><testsuite><testcase name="test_requirement">'
                    '<failure message="assert 1 == 2" /></testcase></testsuite></testsuites>'
                )
                captured["called"] = True
                return 1, False

            with (
                patch.object(
                    harbor_grading,
                    "stage_candidate",
                    return_value=({}, {"state": "candidate-state"}, workspace),
                ),
                patch.object(harbor_grading, "run_regression_process", side_effect=sandbox_process),
                patch.object(
                    harbor_grading, "grade", side_effect=AssertionError("Independent grader called")
                ),
            ):
                result = harbor_grading.regression(root / "candidate.json", authored)
            self.assertTrue(captured["called"])
            self.assertFalse(result["passed"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("assert 1 == 2", result["output"])
            self.assertLessEqual(len(result["output"]), 4000)
            self.assertTrue(result["output_truncated"])
            self.assertEqual(result["state"], "candidate-state")
            self.assertEqual(fixed.read_text(), "# evaluator-owned\n")
            self.assertFalse((root / "reward.txt").exists())

    def test_empty_skipped_malformed_or_timed_out_tests_cannot_report_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            xml = Path(directory) / "junit.xml"
            examples = (
                ("<testsuites><testsuite /></testsuites>", 5, False),
                ('<testsuite><testcase name="skip"><skipped /></testcase></testsuite>', 0, False),
                ("invalid xml", 0, False),
                ('<testsuite><testcase name="pass" /></testsuite>', -9, True),
                ('<testsuite><testcase name="error"><error /></testcase></testsuite>', 2, False),
            )
            for content, code, timed_out in examples:
                with self.subTest(code=code, timeout=timed_out, content=content):
                    xml.write_text(content)
                    self.assertFalse(
                        harbor_grading.regression_status(xml, code, timed_out)["passed"]
                    )


if __name__ == "__main__":
    unittest.main()
