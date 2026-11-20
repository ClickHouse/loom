"""Candidate-written reports cannot authorize objective grading or client I/O."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.cohort import Cohort
from examples.software_factory.controller import run_active
from examples.software_factory.engine import Factory
from examples.software_factory.historical import HistoricalGrader
from examples.software_factory.qualify import qualify
from examples.software_factory.tests.controller_worker import prepare
from examples.software_factory.tests.test_controller import protocol
from examples.software_factory.workspace import GradingBudgetExceeded


class GradingAuthorityContracts(unittest.TestCase):
    def test_historical_qualification_refuses_before_private_inputs_or_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("examples.software_factory.qualify.load_inputs") as load:
                with self.assertRaisesRegex(PermissionError, "candidate-independent"):
                    qualify(
                        "unqualified", Path("unread-private.json"), Path(directory) / "controls"
                    )
                load.assert_not_called()

    def test_factory_rejects_candidate_and_unknown_authority_before_model_io(self):
        for authority in ("candidate", None):
            with self.subTest(authority=authority), tempfile.TemporaryDirectory() as directory:
                task, grader = prepare("name-validation", None)
                grader.result_authority = authority
                provider = Mock()
                output = Path(directory) / "agent"
                with self.assertRaisesRegex(PermissionError, "candidate-independent"):
                    Factory(task, provider, grader, output, mode="baseline")
                provider.step.assert_not_called()
                self.assertFalse(output.exists())

    def test_worker_refuses_untrusted_grader_before_client_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            with Cohort(root, plan) as cohort:
                cohort.begin(lambda _: None)
            task, grader = prepare("name-validation", None)
            grader.result_authority = "candidate"
            client = Mock()
            with self.assertRaisesRegex(PermissionError, "candidate-independent"):
                run_active(
                    root,
                    plan,
                    0,
                    root / "worker",
                    verify_eligibility=lambda _: None,
                    prepare=lambda *_: (task, grader),
                    provider_factory=client,
                )
            client.assert_not_called()

    def test_historical_hidden_report_and_exit_cannot_be_an_objective_grade(self):
        source_files = {
            "src/flask/app.py": "value = 0\n",
            "tests/test_case.py": "def test_case(): assert False\n",
        }
        source = SimpleNamespace(commit="a" * 40, files=lambda: dict(source_files))
        implementation = {k: v for k, v in source_files.items() if k.startswith("src/flask/")}
        checkout = SimpleNamespace(
            original=implementation, files=lambda: implementation, state=lambda: "fixture-state"
        )
        for statuses in ({"tests/test_case.py::test_case": "PASSED"}, {"uncollected": "PASSED"}):
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as directory:
                grader = HistoricalGrader(
                    source,
                    "sha256:" + "a" * 64,
                    ["tests/test_case.py::test_case"],
                    Path(directory) / "evaluation",
                )
                forged = {"passed": True, "selected": statuses, "exit_code": 0}
                with patch(
                    "examples.software_factory.historical.DockerTests.run", return_value=forged
                ) as runner:
                    with self.assertRaisesRegex(PermissionError, "candidate-independent"):
                        grader.run(checkout, hidden=True)
                    runner.assert_not_called()
                self.assertFalse(grader.output.exists())

    def test_historical_visible_diagnostics_keep_base_tests_and_bound_calls(self):
        base = {
            "src/flask/app.py": "value = 0\n",
            "tests/test_case.py": "def test_case(): assert False\n",
        }
        source = SimpleNamespace(commit="a" * 40, files=lambda: dict(base))
        checkout = SimpleNamespace(
            original={"src/flask/app.py": base["src/flask/app.py"]},
            files=lambda: {"src/flask/app.py": "value = 1\n"},
            state=lambda: "edited-state",
        )
        with tempfile.TemporaryDirectory() as directory:
            grader = HistoricalGrader(
                source,
                "sha256:" + "a" * 64,
                ["tests/test_case.py::test_case"],
                Path(directory) / "grade",
            )

            def observe(_runner, workspace):
                self.assertEqual((workspace / "src/flask/app.py").read_text(), "value = 1\n")
                self.assertEqual(
                    (workspace / "tests/test_case.py").read_text(),
                    "def test_case(): assert False\n",
                )
                return {"passed": False, "exit_code": 1}

            with patch(
                "examples.software_factory.historical.DockerTests.run",
                autospec=True,
                side_effect=observe,
            ) as runner:
                for _ in range(3):
                    self.assertFalse(grader.run(checkout)["passed"])
                with self.assertRaises(GradingBudgetExceeded):
                    grader.run(checkout)
                self.assertEqual(runner.call_count, 3)
            self.assertEqual(grader.result_authority, "candidate")


if __name__ == "__main__":
    unittest.main()
