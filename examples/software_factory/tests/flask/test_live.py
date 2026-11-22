"""Contracts needed before any live cohort can be enabled."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.model import Task, digest
from examples.software_factory.factory.workspace import Checkout
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture


class HistoricalSourceContracts(unittest.TestCase):
    def test_early_edit_preserves_the_unread_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = 'value = "old"\n' + "# filler\n" * 3000 + 'sentinel = "kept"\n'
            files = {"src/subject.py": source}
            task = Task(
                "long",
                "historical",
                "pallets/flask",
                "a" * 40,
                "frozen",
                "fix",
                "value",
                files,
                digest(files),
            )
            checkout = Checkout(Path(directory) / "checkout", task)
            checkout.edit("src/subject.py", '"old"', '"new"')
            self.assertEqual(
                checkout.path("src/subject.py").read_text(), source.replace('"old"', '"new"')
            )
            self.assertNotIn('sentinel = "kept"', checkout.diff())

    def test_late_edit_and_paged_read_work_without_exposing_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = "# filler\n" * 3000 + 'value = "old"\n'
            files = {"src/subject.py": source}
            task = Task(
                "long",
                "historical",
                "pallets/flask",
                "a" * 40,
                "frozen",
                "fix",
                "value",
                files,
                digest(files),
            )
            checkout = Checkout(Path(directory) / "checkout", task)
            result = checkout.edit("src/subject.py", '"old"', '"new"')
            self.assertNotIn("error", result)
            self.assertEqual(checkout.read("src/subject.py", start_line=3001), 'value = "new"\n')
            self.assertEqual(
                checkout.state(), digest({"src/subject.py": source.replace('"old"', '"new"')})
            )


class LivePreparationContracts(unittest.TestCase):
    def test_cleanup_cannot_remove_a_foreign_container_label(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from examples.software_factory.harbor.cleanup import remove_owned

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "factory-container.json"
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "factory-container-ownership-v1",
                        "name": "loom-factory-" + "a" * 32,
                        "owner": "a" * 32,
                        "cleaned": False,
                    }
                )
            )
            with patch(
                "examples.software_factory.harbor.cleanup.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="foreign\n"),
            ) as run:
                with self.assertRaises(PermissionError):
                    remove_owned(receipt)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], "loom-factory-" + "a" * 32)
            self.assertFalse(json.loads(receipt.read_text())["cleaned"])

    def test_focused_grading_rejects_missing_skipped_and_duplicate_tests(self):
        from examples.software_factory.evals.flask.historical import (
            junit_statuses,
            selected_statuses,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            path.write_text(
                '<testsuite errors="0"><testcase file="tests/a.py" classname="tests.a.C" '
                'name="test_x[a b]"/><testcase file="tests/a.py" classname="tests.a.C" '
                'name="test_x[c]"><skipped/></testcase></testsuite>'
            )
            statuses = junit_statuses(path)
            self.assertEqual(statuses["tests/a.py::C::test_x[a b]"], "PASSED")
            selected = selected_statuses(statuses, ["tests/a.py::C::test_x", "tests/a.py::missing"])
            self.assertEqual(set(selected.values()), {"ERROR"})
            path.write_text(
                '<testsuite><testcase file="tests/a.py" classname="tests.a" name="test_x"/>'
                '<testcase file="tests/a.py" classname="tests.a" name="test_x"/></testsuite>'
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                junit_statuses(path)

    def test_sandbox_exposes_only_readonly_source_and_results(self):
        from examples.software_factory.evals.flask.historical import DockerTests

        runner = DockerTests("sha256:" + "a" * 64, ["tests/a.py::test_x"], Path("unused"))
        command = runner.command(Path("/task"), Path("/output"), "owned", "owner")
        for flag in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--memory=512m",
        ):
            self.assertIn(flag, command)
        mounts = [command[i + 1] for i, word in enumerate(command) if word == "--mount"]
        self.assertEqual(
            mounts,
            [
                "type=bind,source=/task,target=/workspace,readonly",
                "type=bind,source=/output,target=/results",
            ],
        )
        self.assertEqual(command[-1], "tests/a.py::test_x")
        with self.assertRaises(ValueError):
            DockerTests("latest", ["tests/a.py::test_x"], Path("unused"))
        with self.assertRaises(ValueError):
            DockerTests("sha256:" + "a" * 64, ["tests"], Path("unused"))

    def test_all_frozen_cases_fit_focused_node_budget(self):
        import json

        from examples.software_factory.evals.flask.historical import DockerTests

        candidates = json.loads(
            Path(__file__).parents[2].joinpath("evals/flask/historical-candidates.json").read_text()
        )
        for task in candidates["tasks"]:
            DockerTests(task["dependency_image"], task["objective_selectors"], Path("unused"))

    def test_global_reservation_retains_failures_and_refuses_unknown_cache_billing(self):
        from examples.software_factory.harbor.live_support import Reservations

        budget = Reservations(
            requests=2,
            input_tokens=200,
            output_tokens=20,
            dollars="1",
            input_rate="1",
            output_rate="5",
        )
        budget.reserve(100, 10)
        with self.assertRaises(PermissionError):
            budget.reserve(100, 10)
        with self.assertRaises(ValueError):
            budget.settle(
                {
                    "inputTokens": 50,
                    "outputTokens": 5,
                    "totalTokens": 55,
                    "cacheReadInputTokens": 20,
                }
            )
        self.assertTrue(budget.unknown)
        budget.settle({"inputTokens": 50, "outputTokens": 5, "totalTokens": 55})
        budget.reserve(100, 10)
        self.assertEqual(budget.spent["input_tokens"], 200)
        budget.settle({"inputTokens": 1, "outputTokens": 1, "totalTokens": 2})
        with self.assertRaises(TimeoutError):
            budget.reserve(1, 1)

    def test_persistable_schedule_pairs_all_arms_and_keeps_aa(self):
        from examples.software_factory.harbor.live_support import schedule

        rows = schedule(["4045", "4575", "4935"], seed=741)
        self.assertEqual(rows, schedule(["4045", "4575", "4935"], seed=741))
        self.assertEqual(len(rows), 51)
        self.assertEqual(sum(r["mode"] == "baseline" for r in rows), 15)
        for task in ("4045", "4575", "4935"):
            for repeat in range(3):
                self.assertEqual(
                    {r["mode"] for r in rows if r["task_id"] == task and r["repeat"] == repeat},
                    {"baseline", "natural"},
                )

    def test_scope_verifier_refuses_stale_build_and_restricted_counts(self):
        import asyncio
        from types import SimpleNamespace

        from examples.software_factory.evals.flask.indexing import ScopeVerifier

        scope = {
            "org": "test",
            "namespace": "frozen",
            "repository": "pallets/flask",
            "commit": "a" * 40,
        }
        data = {
            "org": "test",
            "namespace": "frozen",
            "repo": "https://github.com/pallets/flask",
            "git_head": "a" * 40,
            "build_commit": "b" * 40,
            "files_total": 0,
        }

        class Agent:
            async def call_with_meta(self, op, body, **kwargs):
                return SimpleNamespace(data=dict(data), request_id="scope-request")

        verifier = ScopeVerifier(
            scope,
            index_repository=data["repo"],
            build_commit="b" * 40,
            certificate_sha256="c" * 64,
            index_tier="treesitter",
            compiler_rows=dict(
                compiler_definitions=0,
                compiler_references=0,
                python_definitions=0,
                python_references=0,
            ),
        )
        result = asyncio.run(verifier(Agent(), scope))
        self.assertNotIn("empty", result)
        self.assertIsNone(result["cost_usd"])
        for bad in ({"build_commit": "d" * 40}, {"restricted": True}):
            before = dict(data)
            data.update(bad)
            with self.assertRaises(PermissionError):
                asyncio.run(verifier(Agent(), scope))
            data.clear()
            data.update(before)

    def test_exact_source_export_has_no_future_objects_or_reference_patch(self):
        from examples.software_factory.evals.flask.historical import FrozenSource, git

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "--quiet")
            (root / "src/flask").mkdir(parents=True)
            source = root / "src/flask/app.py"
            source.write_text("value = 'base'\n")
            git(root, "add", "src")
            git(
                root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "base",
            )
            base = git(root, "rev-parse", "HEAD").decode().strip()
            source.write_text("secret_future_fix = True\n")
            git(root, "add", "src")
            git(
                root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "future",
            )
            exported = FrozenSource(root, base).files("src/flask")
            self.assertEqual(exported, {"src/flask/app.py": "value = 'base'\n"})


class FailedRequestAccounting(unittest.TestCase):
    def test_failed_provider_attempt_preserves_observed_physical_dispatches(self):
        import json

        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        root = Path(__file__).parents[1] / "fixtures/python"
        entry = json.loads((root / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(root / entry["task"]["path"])
        grader = ObjectiveGrader(root / entry["grader"]["path"], entry["grader"]["sha256"])

        class BrokenProvider:
            label = "injected-failed-transport"

            def step(self, *args):
                failure = RuntimeError("dispatch failed")
                failure.physical_requests = 2
                raise failure

        with tempfile.TemporaryDirectory() as directory:
            result = Factory(
                task, BrokenProvider(), grader, Path(directory) / "attempt", mode="baseline"
            ).run()
            self.assertEqual(result["provider_physical_requests"], 2)
            self.assertEqual(result["provider_failed_requests"], 1)
            self.assertIsNone(result["total_cost_usd"])
            self.assertFalse(result["resolved"])

    def test_invalid_hidden_grader_retains_a_failed_result(self):
        import json

        from examples.software_factory.factory.adapters import ReplayProvider
        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        root = Path(__file__).parents[1] / "fixtures/python"
        entry = json.loads((root / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(root / entry["task"]["path"])

        class BrokenHiddenGrader(ObjectiveGrader):
            def run(self, checkout, *, hidden=False):
                if hidden:
                    raise ValueError("no objective tests collected")
                return super().run(checkout, hidden=hidden)

        grader = BrokenHiddenGrader(root / entry["grader"]["path"], entry["grader"]["sha256"])
        provider = ReplayProvider(root / entry["replay"]["path"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "attempt"
            result = Factory(task, provider, grader, output, mode="baseline").run()
            self.assertFalse(result["resolved"])
            self.assertEqual(result["status"], "invalid_grader")
            self.assertTrue((output / "result.json").is_file())
            self.assertGreater(result["provider_logical_requests"], 0)

    def test_rejected_large_response_still_retains_usage_and_cost(self):
        import json

        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        root = Path(__file__).parents[1] / "fixtures/python"
        entry = json.loads((root / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(root / entry["task"]["path"])
        grader = ObjectiveGrader(root / entry["grader"]["path"], entry["grader"]["sha256"])

        class OversizedProvider:
            label = "injected-large-response"

            def step(self, *args):
                return {
                    "blocks": [{"text": "x" * 24001}],
                    "physical_requests": 1,
                    "usage": {"inputTokens": 100, "outputTokens": 5000},
                    "cost_usd": 0.05,
                    "cost_status": "priced",
                }

        with tempfile.TemporaryDirectory() as directory:
            result = Factory(
                task, OversizedProvider(), grader, Path(directory) / "attempt", mode="baseline"
            ).run()
            self.assertEqual(result["provider_physical_requests"], 1)
            self.assertEqual(result["provider_usage"][0]["outputTokens"], 5000)
            self.assertEqual(result["total_cost_usd"], 0.05)
            self.assertFalse(result["resolved"])


class RuntimeBoundaryContracts(unittest.TestCase):
    def test_http_dispatch_observer_counts_failed_io_and_stops_before_extra_io(self):
        import asyncio

        import httpx

        from examples.software_factory.harbor.live_support import TransportAudit

        audit = TransportAudit(max_requests=2)
        seen = []

        def handle(request):
            seen.append(request.url.path)
            if request.url.path == "/failed":
                raise httpx.ConnectError("injected network failure")
            return httpx.Response(200, json={})

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle),
                event_hooks={"request": [audit.request], "response": [audit.response]},
            ) as client:
                await client.get("https://offline.invalid/ok")
                with self.assertRaises(httpx.ConnectError):
                    await client.get("https://offline.invalid/failed")
                with self.assertRaises(TimeoutError):
                    await client.get("https://offline.invalid/blocked")

        asyncio.run(exercise())
        self.assertEqual(audit.count(), 2)
        self.assertEqual(seen, ["/ok", "/failed"])
        self.assertEqual(sum(row["phase"] == "response" for row in audit.rows), 1)

    def test_hard_controller_deadline_retains_a_timeout_receipt(self):
        import json
        import os
        import sys

        from examples.software_factory.harbor.supervision import run_bounded

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_bounded(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                cwd=root,
                env={"PATH": os.defpath},
                output=root / "run",
                timeout_s=0.05,
            )
            self.assertTrue(result["timed_out"])
            self.assertLess(result["duration_s"], 5)
            self.assertNotEqual(result["exit_code"], 0)
            self.assertEqual(json.loads((root / "run/supervision.json").read_text()), result)

    def test_baseline_intake_exposes_source_discovery_without_private_inputs(self):
        import json

        from examples.software_factory.factory.adapters import ReplayProvider
        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        root = Path(__file__).parents[1] / "fixtures/python"
        entry = json.loads((root / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(root / entry["task"]["path"])
        grader = ObjectiveGrader(root / entry["grader"]["path"], entry["grader"]["sha256"])
        with tempfile.TemporaryDirectory() as directory:
            factory = Factory(
                task,
                ReplayProvider(root / entry["replay"]["path"]),
                grader,
                Path(directory) / "attempt",
                mode="baseline",
            )
            prompt = factory.messages[0]["content"][0]["text"]
            self.assertIn('"source_paths": ["src/subject.py"]', prompt)
            for private in ("graders/", "replays/", "suite.json", "namespace", "Loom"):
                self.assertNotIn(private, prompt)


class ContainerExitContracts(unittest.TestCase):
    def run_case(self, attach_exit):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from examples.software_factory.evals.flask.historical import DockerTests

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            output = root / "test"

            def run(argv, **kwargs):
                if argv[1] == "start":
                    (output / "results/junit.xml").write_text(
                        '<testsuite errors="0"><testcase file="tests/a.py" '
                        'classname="tests.a" name="test_x"><failure/></testcase></testsuite>'
                    )
                state = {"ExitCode": 1, "Running": False, "OOMKilled": False, "Error": ""}
                return SimpleNamespace(
                    returncode=attach_exit if argv[1] == "start" else 0, stdout=json.dumps(state)
                )

            runner = DockerTests("sha256:" + "a" * 64, ["tests/a.py::test_x"], output)
            with (
                patch(
                    "examples.software_factory.evals.flask.historical.subprocess.run",
                    side_effect=run,
                ),
                patch("examples.software_factory.evals.flask.historical.remove_owned"),
            ):
                result = runner.run(workspace)
            return result

    def test_objective_failure_uses_container_exit_not_attach_client_exit(self):
        for attach_exit in (0, 1):
            with self.subTest(attach_exit=attach_exit):
                result = self.run_case(attach_exit)
                self.assertEqual(result["exit_code"], 1)
                self.assertEqual(result["transport_exit_code"], attach_exit)
                self.assertFalse(result["passed"])
        with self.assertRaises(RuntimeError):
            self.run_case(5)
