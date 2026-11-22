"""Authored analysis inputs exercise pairing; they are not benchmark results."""

from __future__ import annotations

import copy
import unittest

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.paired import analyze


def inputs():
    rows = schedule(["a", "b", "c"], seed=741)
    order = {"attempts": rows, "schedule_sha256": digest(rows)}
    entries = []
    for row in rows:
        chosen = 1 if row["mode"] == "natural" else 0
        entries.append(
            {
                "order": row["order"],
                "result": {
                    "task_id": row["task_id"],
                    "mode": row["mode"],
                    "arm": row["arm"],
                    "index_tier": row["index_tier"],
                    "memory_enabled": row["memory_enabled"],
                    "provider": "authored-fixture",
                    "kind": "authored-analysis-contract",
                    "resolved": True,
                    "status": "completed",
                    "duration_s": 5,
                    "total_cost_usd": 2,
                    "cost_status": "priced",
                    "tool_total_requests": 4,
                    "model_selected_loom_requests": chosen,
                    "loom_logical_requests": chosen,
                    "loom_physical_requests": chosen * 3,
                    "provider_logical_requests": 1,
                    "provider_physical_requests": 1,
                    "provider_failed_requests": 0,
                    "memory_rows_written": 0,
                    "provider_usage": [{"inputTokens": 10, "outputTokens": 2}],
                },
            }
        )
    return order, entries


class PairedAnalysisContracts(unittest.TestCase):
    def test_memory_treatment_requires_boolean_identity_and_no_disabled_writes(self):
        order, entries = inputs()
        target = next(e for e in entries if e["result"]["arm"] == "baseline")
        target["result"]["memory_enabled"] = 0
        with self.assertRaisesRegex(ValueError, "identity"):
            analyze(order, entries, resamples=100)
        target["result"]["memory_enabled"] = False
        target["result"]["memory_rows_written"] = 1
        with self.assertRaisesRegex(ValueError, "Memory-disabled"):
            analyze(order, entries, resamples=100)

    def test_baseline_physical_dispatch_is_zero_and_known(self):
        for physical in (1, None):
            with self.subTest(physical=physical):
                order, entries = inputs()
                base = next(e for e in entries if e["result"]["mode"] == "baseline")
                base["result"]["loom_physical_requests"] = physical
                with self.assertRaisesRegex(ValueError, "Baseline"):
                    analyze(order, entries, resamples=100)

    def test_failed_attempt_costs_stay_in_arm_and_whole_cohort(self):
        order, entries = inputs()
        failed = next(
            e for e in entries if order["attempts"][e["order"]]["arm"] == "loom-treesitter"
        )
        failed["result"].update(resolved=False, status="objective_failure", total_cost_usd=10)
        report = analyze(order, entries, resamples=100)
        arm = report["arms"]["loom-treesitter"]
        self.assertEqual(arm["resolved"], 8)
        self.assertEqual(arm["total_cost_usd"], 26)
        self.assertEqual(arm["cost_per_success_usd"], 26 / 8)
        self.assertEqual(report["cohort_total_cost_usd"], 110)
        self.assertEqual(arm["statuses"]["objective_failure"], 1)

    def test_unknowns_are_not_zero_or_removed_from_pairs(self):
        order, entries = inputs()
        next(e for e in entries if e["result"]["arm"] == "loom-treesitter")["result"].update(
            total_cost_usd=5, cost_status="unknown"
        )
        report = analyze(order, entries, resamples=100)
        self.assertIsNone(report["cohort_total_cost_usd"])
        contrast = report["paired_deltas"]["loom-treesitter-minus-baseline"]
        self.assertIsNone(contrast["total_cost_usd"]["estimate"])
        self.assertIsNone(contrast["end_to_end_s"]["estimate"])
        self.assertIsNone(contrast["provider_cacheReadInputTokens"]["estimate"])
        self.assertEqual(contrast["provider_inputTokens"]["estimate"], 0)
        self.assertEqual(contrast["quality"]["estimate"], 0)

    def test_missing_attempt_suppresses_partial_cohort_inference(self):
        order, entries = inputs()
        missing = entries.pop()["order"]
        report = analyze(order, entries, resamples=100)
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing_orders"], [missing])
        self.assertEqual(report["paired_deltas"], {})
        self.assertEqual(sum(a["observed"] for a in report["arms"].values()), 50)

    def test_duplicates_scope_changes_and_baseline_exposure_refuse(self):
        order, entries = inputs()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            analyze(order, entries + [copy.deepcopy(entries[0])], resamples=100)
        bad = copy.deepcopy(entries)
        bad[0]["result"]["task_id"] = "other"
        with self.assertRaisesRegex(ValueError, "identity"):
            analyze(order, bad, resamples=100)
        bad = copy.deepcopy(entries)
        base = next(e for e in bad if e["result"]["mode"] == "baseline")
        base["result"]["loom_logical_requests"] = 1
        with self.assertRaisesRegex(ValueError, "Baseline"):
            analyze(order, bad, resamples=100)

    def test_task_cluster_intervals_and_aa_stay_separate(self):
        order, entries = inputs()
        for entry in entries:
            row, result = order["attempts"][entry["order"]], entry["result"]
            if row["arm"] == "baseline":
                result["resolved"] = False
            if row["arm"] == "loom-treesitter":
                result["resolved"] = row["task_id"] == "a"
            if row["arm"] == "baseline-b":
                result["duration_s"] = 9
        report = analyze(order, entries)
        quality = report["paired_deltas"]["loom-treesitter-minus-baseline"]["quality"]
        self.assertEqual(quality["task_deltas"], {"a": 1, "b": 0, "c": 0})
        self.assertEqual(quality["estimate"], 1 / 3)
        self.assertEqual(quality["ci95"], [0, 1])
        self.assertEqual(
            report["aa_reference"]["baseline-b-minus-baseline-a"]["attempt_duration_s"]["estimate"],
            4,
        )
        self.assertEqual(
            report["paired_deltas"]["loom-treesitter-minus-baseline"]["attempt_duration_s"][
                "estimate"
            ],
            0,
        )
        self.assertFalse(report["benchmark_claims"])
        self.assertTrue(report["exploratory_only"])

    def test_failed_provider_usage_is_unknown_and_tool_calls_avoid_double_count(self):
        order, entries = inputs()
        target = next(e for e in entries if e["result"]["arm"] == "loom-treesitter")["result"]
        target.update(
            provider_failed_requests=1,
            model_selected_loom_requests=1,
            loom_logical_requests=1,
            loom_physical_requests=3,
        )
        report = analyze(order, entries, resamples=100)
        contrast = report["paired_deltas"]["loom-treesitter-minus-baseline"]
        self.assertIsNone(contrast["provider_inputTokens"]["estimate"])
        target_metrics = report["arms"]["loom-treesitter"]["attempts"][0]
        self.assertEqual(target_metrics["total_tool_requests"], 4)

    def test_seed_reproduces_analysis_and_schedule_tampering_refuses(self):
        order, entries = inputs()
        self.assertEqual(
            analyze(order, entries, resamples=100), analyze(order, entries, resamples=100)
        )
        self.assertEqual(
            analyze(order, entries, resamples=100)["paired_deltas"],
            analyze(order, list(reversed(entries)), resamples=100)["paired_deltas"],
        )
        order["attempts"][0]["task_id"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash"):
            analyze(order, entries, resamples=100)

    def test_both_tiers_and_their_direct_contrast_keep_distinct_identities(self):
        order, entries = inputs()
        report = analyze(order, entries, resamples=100)
        self.assertEqual(
            set(report["paired_deltas"]),
            {
                "loom-treesitter-minus-baseline",
                "loom-scip-minus-baseline",
                "loom-scip-minus-loom-treesitter",
                "loom-treesitter-memory-minus-baseline",
                "loom-scip-memory-minus-baseline",
                "loom-treesitter-memory-minus-loom-treesitter",
                "loom-scip-memory-minus-loom-scip",
                "loom-scip-memory-minus-loom-treesitter-memory",
            },
        )
        wrong = copy.deepcopy(entries)
        target = next(e for e in wrong if e["result"]["arm"] == "loom-scip")
        target["result"]["index_tier"] = "treesitter"
        with self.assertRaisesRegex(ValueError, "identity"):
            analyze(order, wrong, resamples=100)

    def test_unbalanced_arm_positions_are_rejected_before_analysis(self):
        order, entries = inputs()
        block = [r for r in order["attempts"] if r["task_id"] == "a" and r["repeat"] == 0]
        left, right = block[:2]
        for key in ("arm", "mode", "index_tier", "memory_enabled"):
            left[key], right[key] = right[key], left[key]
        order["schedule_sha256"] = digest(order["attempts"])
        with self.assertRaisesRegex(ValueError, "distinct positions"):
            analyze(order, entries, resamples=100)


if __name__ == "__main__":
    unittest.main()


class NativeFailureReporting(unittest.TestCase):
    def test_missing_baseline_trajectory_retains_the_attempt_and_unknown_calls(self):
        order, entries = inputs()
        base = next(row for row in entries if row["result"]["mode"] == "baseline")
        base["result"].update(
            resolved=False,
            status="agent_error",
            model_selected_loom_requests=None,
            tool_total_requests=None,
        )
        report = analyze(order, entries, resamples=100)
        self.assertTrue(report["complete"])
        self.assertEqual(report["arms"]["baseline"]["resolved"], 8)
