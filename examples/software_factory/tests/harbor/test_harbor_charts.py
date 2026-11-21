import copy
import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.harbor.charts import METRICS, chart_data, generate_charts


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
                "Memory rows written",
                "Tokens",
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
