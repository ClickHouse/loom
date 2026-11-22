"""Crash, authorization and spending boundaries without network or credentials."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from examples.software_factory.factory.adapters import ReplayProvider
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.model import Budget
from examples.software_factory.harbor.cohort import Cohort, ReservedProvider
from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture
from examples.software_factory.tests.smoke import ROOT


def protocol():
    return {
        "schedule": [{"order": i, "task_id": "fixture", "mode": "baseline"} for i in range(2)],
        "model_reservation": {
            "requests": 2,
            "input_tokens": 20,
            "output_tokens": 10,
            "dollars": "0.01",
            "input_rate": "2.2",
            "output_rate": "11",
        },
    }


class CohortContracts(unittest.TestCase):
    def test_baseline_result_does_not_report_generation_price_as_total_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = json.loads((ROOT / "suite.json").read_text())["tasks"]["name-validation"]
            task = load_fixture(ROOT / frozen["task"]["path"])
            grader = ObjectiveGrader(ROOT / frozen["grader"]["path"], frozen["grader"]["sha256"])
            replay = ReplayProvider(ROOT / frozen["replay"]["path"])

            def dispatch(*args):
                response = replay.step(*args)
                response["usage"] = {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4}
                return response

            plan = protocol()
            plan["model_reservation"].update(requests=20, input_tokens=200, output_tokens=100)
            with Cohort(root / "cohort", plan) as cohort:
                cohort.begin(lambda _: None)
                provider = ReservedProvider(
                    Mock(label="fixture-priced", step=Mock(side_effect=dispatch)),
                    cohort,
                    verified_input_bound=10,
                )
                factory = Factory(
                    task,
                    provider,
                    grader,
                    root / "attempt",
                    mode="baseline",
                    budget=Budget(output_tokens=5),
                )
                result = factory.run()
                self.assertTrue(result["resolved"])
                self.assertEqual(result["memory_rows_written"], 0)
                self.assertIsNone(result["total_cost_usd"])
                self.assertEqual(result["cost_status"], "unknown")
                self.assertTrue(all(r["settled"] for r in cohort.state["accounting"]["rows"]))

    def test_restart_never_retries_an_interrupted_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()) as cohort:
                cohort.begin(lambda _: None)
                cohort.reserve(10, 5)
            with Cohort(root, protocol()) as recovered:
                with self.assertRaisesRegex(PermissionError, "reconciliation"):
                    recovered.begin(Mock())
                self.assertEqual(recovered.state["accounting"]["spent"]["requests"], 1)
                self.assertTrue(recovered.state["accounting"]["unknown"])

    def test_dependency_or_approval_refusal_precedes_attempt_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            with Cohort(Path(directory), protocol()) as cohort:
                refuse = Mock(side_effect=PermissionError("AWS integration unverified"))
                with self.assertRaisesRegex(PermissionError, "AWS"):
                    cohort.begin(refuse)
                self.assertEqual(cohort.state["attempts"][0]["state"], "pending")
                self.assertEqual(cohort.state["accounting"]["spent"]["requests"], 0)

    def test_failed_attempt_cost_and_order_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()) as cohort:
                cohort.begin(lambda _: None)
                request = cohort.reserve(10, 5)
                cohort.settle(request, {"inputTokens": 9, "outputTokens": 2, "totalTokens": 11})
                cohort.finish({"resolved": False, "status": "objective_failure"})
            with Cohort(root, protocol()) as cohort:
                self.assertFalse(cohort.state["attempts"][0]["result"]["resolved"])
                self.assertEqual(cohort.begin(lambda _: None)["order"], 1)
                request = cohort.reserve(10, 5)
                cohort.settle(request, {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2})
                with self.assertRaises(TimeoutError):
                    cohort.reserve(1, 1)
                cohort.finish({"resolved": True})
                self.assertIsNone(cohort.begin(lambda _: None))
                self.assertEqual(cohort.state["accounting"]["spent"]["requests"], 2)
                self.assertEqual(len(cohort.state["accounting"]["rows"]), 2)

    def test_second_controller_and_changed_protocol_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()):
                with self.assertRaises(BlockingIOError):
                    Cohort(root, protocol())
            changed = protocol()
            changed["model_reservation"]["requests"] = 3
            with self.assertRaises(PermissionError):
                Cohort(root, changed)

    def test_partial_journal_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()):
                pass
            path = root / "cohort.jsonl"
            with path.open("a") as stream:
                stream.write('{"incomplete":')
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                Cohort(root, protocol())
            self.assertEqual(path.read_bytes(), original)

    def test_provider_can_observe_reservation_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()) as cohort:
                cohort.begin(lambda _: None)

                def dispatch(*_):
                    last = json.loads((root / "cohort.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(last["event"], "request_reserved")
                    return {
                        "blocks": [{"text": "done"}],
                        "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
                        "physical_requests": 1,
                        "provider_request_id": "request-fixture",
                    }

                provider = Mock(label="fake", step=Mock(side_effect=dispatch))
                response = ReservedProvider(provider, cohort, verified_input_bound=10).step(
                    [], [], 5
                )
                self.assertIsNone(response["cost_usd"])
                self.assertGreater(response["generation_cost_usd"], 0)
                self.assertFalse(cohort.state["accounting"]["unknown"])

    def test_unpriced_usage_retains_raw_response_and_stops_next_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()) as cohort:
                cohort.begin(lambda _: None)
                provider = Mock(label="fake")
                provider.step.return_value = {
                    "usage": {"inputTokens": 2, "outputTokens": 1, "cacheReadInputTokens": 5},
                    "provider_request_id": "unpriced-fixture",
                    "physical_requests": 1,
                }
                metered = ReservedProvider(provider, cohort, verified_input_bound=10)
                with self.assertRaisesRegex(ValueError, "pricing"):
                    metered.step([], [], 5)
                self.assertIn("cacheReadInputTokens", (root / "cohort.jsonl").read_text())
                with self.assertRaises(PermissionError):
                    metered.step([], [], 5)
                self.assertEqual(provider.step.call_count, 1)

    def test_failed_durable_write_prevents_dispatch_and_same_process_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with Cohort(Path(directory), protocol()) as cohort:
                cohort.begin(lambda _: None)
                provider = Mock(label="fake")
                metered = ReservedProvider(provider, cohort, verified_input_bound=10)
                with patch(
                    "examples.software_factory.harbor.cohort.os.fsync", side_effect=OSError("disk")
                ):
                    with self.assertRaises(OSError):
                        metered.step([], [], 5)
                with self.assertRaises(RuntimeError):
                    metered.step([], [], 5)
                provider.step.assert_not_called()

    def test_provider_failure_is_not_retried_and_keeps_observed_dispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Cohort(root, protocol()) as cohort:
                cohort.begin(lambda _: None)
                error = RuntimeError("provider unavailable")
                error.physical_requests = 1
                provider = Mock(label="fake", step=Mock(side_effect=error))
                metered = ReservedProvider(provider, cohort, verified_input_bound=10)
                with self.assertRaises(RuntimeError):
                    metered.step([], [], 5)
                cohort.finish({"resolved": False, "status": "provider_failure"})
            with Cohort(root, protocol()) as cohort:
                with self.assertRaises(PermissionError):
                    cohort.begin(lambda _: None)
                events = [
                    json.loads(line) for line in (root / "cohort.jsonl").read_text().splitlines()
                ]
                failure = next(r for r in events if r["event"] == "request_failed")
                self.assertEqual(failure["physical_requests"], 1)
                self.assertEqual(provider.step.call_count, 1)


if __name__ == "__main__":
    unittest.main()
