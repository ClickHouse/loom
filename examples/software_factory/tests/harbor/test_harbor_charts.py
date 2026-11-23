import copy
import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.harbor.charts import (
    METRICS,
    chart_data,
    generate_charts,
    paired_orders,
)


class HarborCharts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.protocol = {
            "output_root": str(self.root / "study"),
            "model_id": "same-model",
            "schedule": [
                {"order": i, "task_id": "public-issue", "repeat": 0, "arm": arm}
                for i, arm in enumerate(
                    (
                        "baseline",
                        "loom-treesitter",
                        "loom-treesitter-memory",
                        "loom-scip",
                        "loom-scip-memory",
                        "baseline-a",
                    )
                )
            ],
            "reporting": {"chart_assets_root": str(self.root / "static/assets")},
        }
        self.views = {
            "views": {
                arm: {"job_name": arm}
                for arm in (
                    "baseline",
                    "loom-treesitter",
                    "loom-treesitter-memory",
                    "loom-scip",
                    "loom-scip-memory",
                )
            }
        }
        self.entries = [
            {
                "order": 0,
                "result": {
                    "resolved": True,
                    "harbor_reward_valid": True,
                    "end_to_end_s": 12.0,
                    "harbor_wall_s": 11.0,
                    "generation_cost_usd": 0.02,
                    "total_provider_cost_usd": 0.02,
                    "loom_provider_cost_usd": 0,
                    "harbor_trial": str(self.root / "raw-trial"),
                },
            }
        ]

    def test_primary_rows_preserve_exact_metrics_and_pending_unknowns(self):
        before = copy.deepcopy(self.entries)
        data = chart_data(self.protocol, self.entries, self.views)
        self.assertEqual(len(data["attempts"]), 5)
        self.assertEqual(data["attempts"][0]["seconds"], 12.0)
        self.assertEqual(data["attempts"][0]["native_seconds"], 11.0)
        self.assertEqual(data["attempts"][0]["cost"], 0.02)
        self.assertIsNone(data["attempts"][1]["cost"])
        self.assertIsNone(data["attempts"][1]["resolved"])
        self.assertEqual(data["attempts"][1]["state"], "pending")
        self.assertEqual(self.entries, before)
        self.assertEqual(data["observed"], 1)
        self.assertEqual(data["scheduled"], 6)
        self.assertFalse(data["complete"])
        self.assertEqual(data["run_status"], "partial")

    def test_all_requested_metrics_preserve_known_zero_and_unknown_evaluations(self):
        self.assertEqual(
            [m["title"] for m in METRICS],
            [
                "Solved (hidden tests)",
                "Fixed, per blind judge",
                "Regressions in full suite",
                "Cost",
                "Agent time",
                "Test runs by the agent",
                "Loom tool calls",
                "Total tool calls",
                "Memory rows written",
                "Solver LLM tokens",
                "Loom LLM tokens",
                "Total LLM tokens",
                "Loom embedding tokens",
            ],
        )
        self.entries[0]["result"].update(
            agent_time_s=4.25,
            agent_test_runs=2,
            loom_tool_calls=0,
            memory_rows_written=0,
            full_suite_regressions=0,
        )
        data = chart_data(self.protocol, self.entries, self.views)
        row = data["attempts"][0]
        self.assertEqual(row["agent_seconds"], 4.25)
        self.assertEqual(row["agent_test_runs"], 2)
        for key in ("loom_tool_calls", "memory_rows_written", "full_suite_regressions"):
            self.assertEqual(row[key], 0)
        for key in ("judge_fixed", "judge_better"):
            self.assertIsNone(row[key])
        self.assertEqual(data["metrics"], METRICS)

    def test_invalid_cost_is_unknown_and_grading_errors_are_not_successes(self):
        self.entries[0]["result"].update(
            harbor_reward_valid=False,
            resolved=True,
            generation_cost_usd=float("nan"),
            total_provider_cost_usd=float("nan"),
        )
        row = chart_data(self.protocol, self.entries, self.views)["attempts"][0]
        self.assertEqual(row["state"], "error")
        self.assertFalse(row["resolved"])
        self.assertIsNone(row["cost"])

    def test_authored_and_historical_experiments_have_separate_chart_cohorts(self):
        self.protocol["tasks"] = {
            "public-issue": {"scenario_family": "historical-issue"},
            "authored": {"scenario_family": "authored-simulated-memory-history"},
        }
        self.protocol["schedule"].append(
            {"order": 6, "task_id": "authored", "repeat": 0, "arm": "baseline"}
        )
        data = chart_data(self.protocol, self.entries, self.views)
        self.assertEqual(data["cohorts"]["default"], "historical-issue")
        self.assertFalse(data["cohorts"]["pooled_headline"])
        self.assertEqual(
            data["cohorts"]["groups"]["historical-issue"]["task_ids"], ["public-issue"]
        )
        self.assertEqual(len(data["attempts"]), 6)

    def test_native_assets_and_navigation_install_without_modifying_raw_records(self):
        shell = self.root / "static/index.html"
        shell.parent.mkdir(parents=True)
        original = "<html><body>Native Harbor</body></html>"
        shell.write_text(original)
        raw = self.root / "raw-trial/result.json"
        raw.parent.mkdir()
        raw.write_text('{"original": true}')
        before = raw.read_bytes()
        result = generate_charts(self.protocol, self.entries, self.views)
        self.assertTrue(result["published"])
        self.assertIn("/assets/factory-charts/", result["url"])
        self.assertTrue(Path(result["html"]).exists())
        target = self.root / "static/assets/factory-charts"
        self.assertEqual((target / "viewer-index.before-charts.txt").read_text(), original)
        marker = '<script defer src="/assets/factory-charts/navigation.js"></script>'
        self.assertEqual(shell.read_text().count(marker), 1)
        generate_charts(self.protocol, self.entries, self.views)
        self.assertEqual(shell.read_text().count(marker), 1)
        self.assertEqual(raw.read_bytes(), before)

    def test_disabled_publication_still_generates_interactive_local_report(self):
        self.protocol["reporting"]["publish_charts"] = False
        result = generate_charts(self.protocol, self.entries, self.views)
        self.assertFalse(result["published"])
        self.assertFalse((self.root / "static").exists())
        html = Path(result["html"]).read_text()
        self.assertIn('id="issue"', html)
        self.assertIn('id="repeat"', html)
        self.assertIn('id="timing"', html)
        self.assertEqual(json.loads(Path(result["data"]).read_text())["attempts"][0]["cost"], 0.02)

    def test_interrupted_report_keeps_observed_counts_and_scheduled_unknowns(self):
        self.views.update(run_status="interrupted", interruption_reason="Setup evidence changed")
        data = chart_data(self.protocol, self.entries, self.views)
        self.assertEqual(data["run_status"], "interrupted")
        self.assertEqual(data["interruption_reason"], "Setup evidence changed")
        self.assertFalse(data["complete"])
        self.assertEqual(data["observed"], 1)
        self.assertEqual(data["scheduled"], 6)
        self.assertIsNone(data["attempts"][1]["resolved"])
        self.protocol["reporting"]["publish_charts"] = False
        html = Path(generate_charts(self.protocol, self.entries, self.views)["html"]).read_text()
        for element in ("run-status", "summary-body", "issues-body", "visual-comparison"):
            self.assertIn(f'id="{element}"', html)
        self.assertNotIn("<details open", html)

    def test_completed_attempts_remain_judging_until_evaluation_finishes(self):
        entries = [
            {"order": slot["order"], "result": copy.deepcopy(self.entries[0]["result"])}
            for slot in self.protocol["schedule"]
        ]
        self.views["evaluation_complete"] = False
        data = chart_data(self.protocol, entries, self.views)
        self.assertTrue(data["complete"])
        self.assertFalse(data["evaluation_complete"])
        self.assertEqual(data["run_status"], "grading")
        self.views["evaluation_complete"] = True
        self.assertEqual(chart_data(self.protocol, entries, self.views)["run_status"], "complete")

    def test_comparison_requires_every_arm_in_the_same_task_and_repeat(self):
        arms = list(self.views["views"])
        rows = [
            dict(order=i, task="one", repeat=0, arm=arm, state="complete", resolved=i != 0)
            for i, arm in enumerate(arms)
        ]
        self.assertEqual(paired_orders(rows, arms), set(range(5)))
        incomplete = copy.deepcopy(rows)
        incomplete[-1]["state"] = "pending"
        self.assertEqual(paired_orders(incomplete, arms), set())
        incomplete[-1]["state"] = "error"
        self.assertEqual(paired_orders(incomplete, arms), set())
        different_repeat = copy.deepcopy(rows)
        different_repeat[-1]["repeat"] = 1
        self.assertEqual(paired_orders(different_repeat, arms), set())
        self.assertEqual(paired_orders(rows + [copy.deepcopy(rows[0])], arms), set())
        self.assertFalse(rows[0]["resolved"])

    def test_raw_attempts_remain_visible_while_only_complete_blocks_are_compared(self):
        entries = [
            {"order": slot["order"], "result": copy.deepcopy(self.entries[0]["result"])}
            for slot in self.protocol["schedule"][:5]
        ]
        entries[1]["result"]["resolved"] = False
        data = chart_data(self.protocol, entries[:-1], self.views)
        self.assertEqual(data["observed"], 4)
        self.assertFalse(any(row["comparison_included"] for row in data["attempts"]))
        self.assertEqual(data["attempts"][0]["cost"], 0.02)
        data = chart_data(self.protocol, entries, self.views)
        self.assertTrue(all(row["comparison_included"] for row in data["attempts"]))
        self.assertFalse(data["attempts"][1]["resolved"])
        self.assertFalse(data["complete"])

    def test_public_titles_cannot_close_the_embedded_json_script(self):
        public = self.root / "public.json"
        public.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"task_id": "public-issue", "problem": "</script><script>bad()</script>"}
                    ]
                }
            )
        )
        self.protocol["historical_manifest"] = str(public)
        self.protocol["reporting"]["publish_charts"] = False
        result = generate_charts(self.protocol, self.entries, self.views)
        html = Path(result["html"]).read_text()
        self.assertNotIn("</script><script>bad()", html)
        self.assertIn("\\u003c/script>", html)
