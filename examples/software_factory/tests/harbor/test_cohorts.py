"""Authored successes cannot change historical issue comparisons."""

import copy
import unittest

from examples.software_factory.harbor.arms import validate_schedule
from examples.software_factory.harbor.cohorts import comparison_cohorts
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.paired import analyze_cohorts
from examples.software_factory.tests.harbor.test_paired import inputs


class CohortComparisons(unittest.TestCase):
    def test_each_experiment_has_balanced_positions_and_complete_pairs(self):
        families = {str(i): "historical-issue" if i < 3 else "authored" for i in range(6)}
        for seed in (741, 0, 13):
            rows = schedule(list(families), seed=seed, task_families=families)
            self.assertEqual(len(validate_schedule(rows)), 6)
            for family in set(families.values()):
                selected = [row for row in rows if families[row["task_id"]] == family]
                normalized = [{**row, "order": i} for i, row in enumerate(selected)]
                self.assertEqual(len(validate_schedule(normalized)), 3)

    def test_outcomes_do_not_choose_the_primary_cohort_or_hide_failures(self):
        order, entries = inputs()
        protocol = {
            "schedule": order["attempts"],
            "tasks": {
                task: {"scenario_family": "historical-issue" if task == "a" else "authored"}
                for task in ("a", "b", "c")
            },
        }
        # Regenerate a cohort-balanced schedule while retaining each paired result.
        rows = schedule(
            ["a", "b", "c"],
            seed=741,
            task_families={t: c["scenario_family"] for t, c in protocol["tasks"].items()},
        )
        records = {
            (row["task_id"], row["repeat"], row["arm"]): entry["result"]
            for row, entry in zip(order["attempts"], entries, strict=True)
        }
        entries = [
            {
                "order": row["order"],
                "result": copy.deepcopy(records[row["task_id"], row["repeat"], row["arm"]]),
            }
            for row in rows
        ]
        protocol["schedule"] = rows
        for entry in entries:
            if entry["result"]["task_id"] == "a":
                entry["result"].update(resolved=False, status="objective_failure")
        report = analyze_cohorts(protocol, entries)
        self.assertEqual(report["primary_cohort"], "historical-issue")
        self.assertFalse(report["pooled_headline"])
        historical = report["cohorts"]["historical-issue"]
        self.assertEqual(historical["cluster_count"], 1)
        self.assertTrue(historical["complete"])
        self.assertEqual(historical["arms"]["baseline"]["resolved"], 0)
        self.assertEqual(historical["arms"]["baseline"]["total_cost_usd"], 6)
        self.assertEqual(len(historical["original_orders"]), 17)
        self.assertEqual(
            sum(len(c["original_orders"]) for c in report["cohorts"].values()), len(rows)
        )

    def test_unknown_family_is_not_presented_as_historical(self):
        result = comparison_cohorts({"schedule": [{"task_id": "unknown"}]})
        self.assertEqual(result["default"], "unclassified")


if __name__ == "__main__":
    unittest.main()
