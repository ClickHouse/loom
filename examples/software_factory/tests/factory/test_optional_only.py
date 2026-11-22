import unittest

from examples.software_factory.factory.model import MODES, tool_schemas
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture


class OptionalOnly(unittest.TestCase):
    def test_only_baseline_and_model_selected_loom_modes_exist(self):
        self.assertEqual(MODES, ("baseline", "natural"))
        self.assertEqual(
            {t["name"] for t in tool_schemas("natural")}
            - {t["name"] for t in tool_schemas("baseline")},
            {
                "loom_code_symbol",
                "loom_code_grep",
                "loom_code_search",
                "loom_code_outline",
                "loom_code_enclosing",
            },
        )

    def test_schedule_has_forty_five_primary_and_six_separate_aa_attempts(self):
        rows = schedule(["a", "b", "c"], seed=741)
        self.assertEqual(len(rows), 51)
        self.assertEqual(sum(r["repeat"] == "aa" for r in rows), 6)
        self.assertEqual({r["mode"] for r in rows}, {"baseline", "natural"})
        self.assertEqual(rows, schedule(["a", "b", "c"], seed=741))

    def test_timing_and_test_counts_exclude_hidden_verification_and_count_failed_choices(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.factory.model import Task, digest

        clock = [1000.0]
        source = {"src/a.py": "def f():\n    return 1\n"}
        task = Task(
            "public",
            "authored-expression-fixture",
            "public/repo",
            "a" * 40,
            "test",
            "Fix public behavior",
            "f",
            source,
            digest(source),
        )

        class Grader:
            sha256 = "grader"
            supports_regression_tests = True

            def run(self, checkout, hidden=False):
                clock[0] += 100 if hidden else 1
                return {"passed": True}

            def regression(self, checkout, source):
                clock[0] += 2
                return {"passed": False, "detail": "agent-authored check failed"}

        class Provider:
            label = "scripted-replay"
            chosen = False

            def step(self, *args):
                clock[0] += 3
                blocks = []
                if not self.chosen:
                    self.chosen = True
                    blocks = [
                        {"toolUse": {"toolUseId": str(i), "name": name, "input": data}}
                        for i, (name, data) in enumerate(
                            [
                                ("test", {}),
                                ("regression_test", {"source": "public regression"}),
                                ("regression_test", {"invalid": "argument"}),
                            ]
                        )
                    ]
                return dict(blocks=blocks, usage={}, physical_requests=0, cost_usd=0)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "examples.software_factory.factory.engine.time.monotonic",
                side_effect=lambda: clock[0],
            ),
        ):
            factory = Factory(task, Provider(), Grader(), Path(directory) / "run", mode="baseline")
            result = factory.run()
        self.assertEqual(result["agent_time_s"], 12)
        self.assertEqual(result["duration_s"], 112)
        self.assertEqual(result["completion_reviews"], 1)
        self.assertEqual(result["provider_logical_requests"], 3)
        self.assertEqual(result["agent_test_runs"], 2)
        self.assertEqual(result["agent_test_requests"], 3)
        self.assertEqual(result["automatic_final_test_runs"], 0)
        self.assertEqual(result["tool_failed_requests"], 1)
        self.assertEqual(result["loom_tool_calls"], 0)
        self.assertEqual(result["memory_rows_written"], 0)
        self.assertIn("read-only", result["memory_rows_written_basis"])

    def test_removed_mode_is_rejected_without_a_compatibility_path(self):
        import json
        import tempfile
        from pathlib import Path

        from examples.software_factory.factory.adapters import ReplayContext, ReplayProvider
        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader
        from examples.software_factory.tests.smoke import ROOT

        case = json.loads((ROOT / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(ROOT / case["task"]["path"])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            Factory(
                task,
                ReplayProvider(ROOT / case["replay"]["path"]),
                ObjectiveGrader(ROOT / case["grader"]["path"], case["grader"]["sha256"]),
                Path(directory) / "run",
                mode="orchestrated",
                backend=ReplayContext(task),
            )

    def test_refused_requests_and_timed_out_runs_have_distinct_counters(self):
        from examples.software_factory.factory.model import TestRefusal
        from examples.software_factory.tests.factory.test_factory import FactoryContracts

        fixture = FactoryContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        factory = fixture.factory(mode="baseline")
        factory.grader.supports_regression_tests = True
        # Build the live tool capability explicitly; offline expression grading stays bounded.
        from examples.software_factory.factory.model import tool_schemas

        factory.tools = tool_schemas("baseline", regression_tests=True)

        def regression(checkout, source):
            if source == "invalid":
                raise TestRefusal("syntax refused before execution")
            return {"passed": False, "status": "timed_out"}

        factory.grader.regression = regression

        class Provider:
            label = "scripted-replay"
            selected = False

            def step(self, *args):
                blocks = []
                if not self.selected:
                    self.selected = True
                    blocks = [
                        {
                            "toolUse": dict(
                                toolUseId=str(i), name="regression_test", input={"source": source}
                            )
                        }
                        for i, source in enumerate(["invalid", "timeout"])
                    ]
                return dict(blocks=blocks, usage={}, physical_requests=0, cost_usd=0)

        factory.provider = Provider()
        result = factory.run()
        self.assertEqual(result["agent_test_requests"], 2)
        self.assertEqual(result["agent_test_runs"], 1)
        self.assertEqual(result["automatic_final_test_runs"], 1)
        self.assertEqual(
            len([r for r in factory.ledger.rows if r["kind"] == "authored_verification"]), 1
        )

    def test_unconfirmed_grader_execution_remains_unknown(self):
        from examples.software_factory.tests.factory.test_factory import FactoryContracts

        fixture = FactoryContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        factory = fixture.factory(mode="baseline")

        class Provider:
            label = "scripted-replay"

            def step(self, *args):
                return dict(
                    blocks=[{"toolUse": dict(toolUseId="test", name="test", input={})}],
                    usage={},
                    physical_requests=0,
                    cost_usd=0,
                )

        factory.provider = Provider()

        def fail(checkout, hidden=False):
            raise RuntimeError("Execution transport ended without a grader receipt")

        factory.grader.run = fail
        result = factory.run(external_verifier=True)
        self.assertEqual(result["agent_test_requests"], 1)
        self.assertIsNone(result["agent_test_runs"])

    def test_operator_named_symbol_never_enters_shared_model_prompt(self):
        from dataclasses import replace

        from examples.software_factory.factory.adapters import ReplayContext, ReplayProvider
        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.factory.test_factory import ROOT, FactoryContracts
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        fixture = FactoryContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = replace(fixture.task, symbol="OPERATOR_ONLY_SENTINEL")
        prompts = []
        for mode in ["baseline", "natural"]:
            factory = Factory(
                task,
                ReplayProvider(ROOT / fixture.frozen["replay"]["path"]),
                ObjectiveGrader(
                    ROOT / fixture.frozen["grader"]["path"], fixture.frozen["grader"]["sha256"]
                ),
                fixture.root / mode,
                mode=mode,
                backend=ReplayContext(task) if mode == "natural" else None,
            )
            prompts.append(factory.messages)
            self.assertNotIn("OPERATOR_ONLY_SENTINEL", str(factory.messages))
        self.assertEqual(prompts[0], prompts[1])
