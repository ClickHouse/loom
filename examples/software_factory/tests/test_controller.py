"""Real supervised subprocesses with authored replay clients, no network."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from examples.software_factory.cohort import Cohort
from examples.software_factory.controller import run_active, run_next
from examples.software_factory.model import Budget, digest
from examples.software_factory.tests.controller_worker import prepare


def protocol(mode="baseline"):
    task, grader = prepare("name-validation", None)
    return {
        "schedule": [{"order": i, "task_id": task.task_id, "mode": mode} for i in range(2)],
        "model_reservation": {
            "requests": 50,
            "input_tokens": 5000,
            "output_tokens": 102400,
            "dollars": "2",
            "input_rate": "2.2",
            "output_rate": "11",
        },
        "tasks": {
            task.task_id: {
                "task_id": task.task_id,
                "kind": task.kind,
                "repository": task.repository,
                "base_commit": task.base_commit,
                "namespace": task.namespace,
                "source_sha256": task.source_sha256,
                "grader_sha256": grader.sha256,
                "problem_sha256": digest(task.problem),
                "symbol": task.symbol,
            }
        },
        "verified_input_bound": 100,
        "attempt_budget": vars(Budget()),
        "org": "fixture",
        "provider_label": "scripted-replay",
        "controller_timeout_s": 5,
        "transport_limits": {"provider": 50, "loom": 30},
    }


def run(root, plan, behavior="complete", verify=None):
    root.mkdir(exist_ok=True)
    (root / "protocol.json").write_text(json.dumps(plan))
    return run_next(
        root,
        plan,
        verify_eligibility=verify or (lambda _: None),
        worker_command=lambda slot, output: [
            sys.executable,
            "-m",
            "examples.software_factory.tests.controller_worker",
            str(root),
            str(output),
            str(slot["order"]),
            behavior,
        ],
        cwd=Path(__file__).resolve().parents[3],
        env={"PATH": os.defpath},
    )


class ControllerContracts(unittest.TestCase):
    def test_durable_transport_caps_survive_restart_and_baseline_refuses_loom(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            plan["transport_limits"]["provider"] = 1
            with Cohort(root, plan) as cohort:
                cohort.begin(lambda _: None)
                with self.assertRaisesRegex(PermissionError, "Baseline"):
                    cohort.transport("loom", {"phase": "dispatch"})
                cohort.transport("provider", {"phase": "dispatch"})
                cohort.finish({"resolved": False})
            with Cohort(root, plan) as cohort:
                cohort.begin(lambda _: None)
                with self.assertRaisesRegex(TimeoutError, "cap exhausted"):
                    cohort.transport("provider", {"phase": "dispatch"})
                self.assertEqual(cohort.state["transport_counts"], {"provider": 1, "loom": 0})

    def test_real_subprocess_cold_baselines_reserve_edit_grade_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            for order in range(2):
                entry = run(root, plan)
                self.assertEqual(entry["order"], order)
                self.assertTrue(entry["result"]["resolved"])
                self.assertEqual(entry["result"]["loom_logical_requests"], 0)
                self.assertIsNone(entry["result"]["total_cost_usd"])
                self.assertGreaterEqual(
                    entry["result"]["end_to_end_s"], entry["result"]["duration_s"]
                )
                output = root / f"attempt-{order:03d}" / "worker"
                self.assertTrue((output / "provider-closed").exists())
                self.assertFalse((output / "context-closed").exists())
                events = [
                    json.loads(s) for s in (output / "agent/events.jsonl").read_text().splitlines()
                ]
                self.assertTrue(
                    any(r["kind"] == "verification" and not r["passed"] for r in events)
                )
                self.assertTrue(any(r["kind"] == "verification" and r["passed"] for r in events))
            self.assertIsNone(run(root, plan))
            with Cohort(root, plan) as cohort:
                self.assertTrue(all(r["settled"] for r in cohort.state["accounting"]["rows"]))
                self.assertTrue(all(r["state"] == "finished" for r in cohort.state["attempts"]))

    def test_host_context_is_constructed_and_closed_without_selected_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run(root, protocol("orchestrated"))["result"]
            self.assertTrue(result["resolved"])
            self.assertGreater(result["host_loom_requests"], 0)
            self.assertEqual(result["model_selected_loom_requests"], 0)
            self.assertTrue((root / "attempt-000/worker/context-closed").exists())

    def test_worker_crash_preserves_reservation_and_refuses_automatic_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            result = run(root, plan, "crash")["result"]
            self.assertFalse(result["resolved"])
            self.assertEqual(result["status"], "controller_failure")
            self.assertIsNone(result["total_cost_usd"])
            with Cohort(root, plan) as cohort:
                self.assertTrue(cohort.state["accounting"]["unknown"])
                self.assertEqual(cohort.state["accounting"]["spent"]["requests"], 1)
            with self.assertRaisesRegex(PermissionError, "reconciliation"):
                run(root, plan)

    def test_hard_timeout_stops_only_owned_worker_and_retains_failed_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            plan["controller_timeout_s"] = 1
            result = run(root, plan, "hang")["result"]
            self.assertFalse(result["resolved"])
            receipt = json.loads((root / "attempt-000/supervision.json").read_text())
            self.assertTrue(receipt["timed_out"])
            pid = int((root / "attempt-000/worker-pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cleanup_failure_does_not_promote_agent_grade_to_completed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run(root, protocol(), "cleanup-failure")["result"]
            self.assertFalse(result["resolved"])
            agent = json.loads((root / "attempt-000/worker/agent/result.json").read_text())
            self.assertTrue(agent["resolved"])
            self.assertFalse((root / "attempt-000/worker/completed.json").exists())
            with self.assertRaisesRegex(PermissionError, "reconciliation"):
                run(root, protocol())

    def test_parent_eligibility_refusal_does_not_start_worker_or_consume_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            with self.assertRaisesRegex(PermissionError, "approval"):
                run(root, plan, verify=Mock(side_effect=PermissionError("approval missing")))
            self.assertFalse((root / "attempt-000").exists())
            with Cohort(root, plan) as cohort:
                self.assertEqual(cohort.state["attempts"][0]["state"], "pending")

    def test_visible_grader_failure_halts_even_with_settled_usage_and_hidden_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan = Path(directory), protocol()
            result = run(root, plan, "visible-grader-failure")["result"]
            self.assertEqual(result["status"], "invalid_grader")
            self.assertFalse(result["resolved"])
            events = [
                json.loads(row)
                for row in (root / "attempt-000/worker/agent/events.jsonl").read_text().splitlines()
            ]
            hidden = [row for row in events if row["kind"] == "hidden_verification"]
            self.assertEqual([row["passed"] for row in hidden], [True])
            with Cohort(root, plan) as cohort:
                self.assertTrue(cohort.state["halted"])
                self.assertFalse(cohort.state["accounting"]["unknown"])
                self.assertTrue(all(r["settled"] for r in cohort.state["accounting"]["rows"]))
                self.assertEqual(cohort.state["attempts"][1]["state"], "pending")
            with self.assertRaisesRegex(PermissionError, "reconciliation"):
                run(root, plan)

    def test_worker_rechecks_eligibility_and_task_before_constructing_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root, plan, client = Path(directory), protocol(), Mock()
            with Cohort(root, plan) as cohort:
                cohort.begin(lambda _: None)
            arguments = {
                "prepare": prepare,
                "provider_factory": client,
                "context_factory": client,
            }
            with self.assertRaisesRegex(PermissionError, "scope"):
                run_active(
                    root,
                    plan,
                    0,
                    root / "worker-refused",
                    verify_eligibility=Mock(side_effect=PermissionError("scope missing")),
                    **arguments,
                )
            task, grader = prepare("name-validation", None)
            task.files["src/subject.py"] = "tampered = True\n"
            arguments["prepare"] = lambda *_: (task, grader)
            with self.assertRaisesRegex(PermissionError, "differs"):
                run_active(
                    root,
                    plan,
                    0,
                    root / "worker-mismatch",
                    verify_eligibility=lambda _: None,
                    **arguments,
                )
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
