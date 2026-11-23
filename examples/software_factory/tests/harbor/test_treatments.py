"""The scheduled memory treatment cannot be confused with code-only retrieval."""

import copy
import unittest
from collections import Counter

from examples.software_factory.harbor.arms import (
    CONTROLS,
    PRIMARY,
    contrasts,
    validate_schedule,
    with_cache_pairs,
)
from examples.software_factory.harbor.charts import paired_orders
from examples.software_factory.harbor.judging import pair_slots
from examples.software_factory.harbor.live_support import schedule


class TreatmentContracts(unittest.TestCase):
    def test_explicit_pilot_requires_exactly_one_task_and_all_five_arms(self):
        full = schedule(["a", "b", "c"], seed=741)
        rows = [
            {**row, "order": i, "pilot": True}
            for i, row in enumerate(r for r in full if r["task_id"] == "a" and r["repeat"] == 0)
        ]
        self.assertEqual(validate_schedule(rows), ["a"])
        for bad in (
            rows[:-1],
            rows + [rows[0]],
            [{k: v for k, v in r.items() if k != "pilot"} for r in rows],
        ):
            with self.assertRaises(ValueError):
                validate_schedule(bad)
        altered = copy.deepcopy(rows)
        altered[0]["task_id"] = "b"
        with self.assertRaises(ValueError):
            validate_schedule(altered)
        altered = copy.deepcopy(rows)
        altered[0]["pilot"] = 1
        with self.assertRaises(ValueError):
            validate_schedule(altered)

    def test_six_families_keep_all_arms_repeats_and_controls(self):
        tasks = [f"scenario-{i}" for i in range(6)]
        rows = schedule(tasks, seed=741)
        self.assertEqual(len(rows), 102)
        self.assertEqual(validate_schedule(rows), tasks)
        self.assertEqual(rows, schedule(list(reversed(tasks)), seed=741))
        self.assertEqual(
            Counter(row["arm"] for row in rows),
            {
                **dict.fromkeys(PRIMARY, 18),
                "baseline-a": 6,
                "baseline-b": 6,
            },
        )
        for arm in PRIMARY:
            positions = Counter()
            for task in tasks:
                for repeat in range(3):
                    block = [r for r in rows if r["task_id"] == task and r["repeat"] == repeat]
                    positions.update(i for i, row in enumerate(block) if row["arm"] == arm)
            self.assertEqual(sorted(positions.values()), [3, 3, 4, 4, 4])

    def test_cold_and_warm_schedule_keeps_blocks_and_controls_separate(self):
        tasks = ["a", "b", "c"]
        rows = with_cache_pairs(schedule(tasks, seed=741))
        self.assertEqual(validate_schedule(rows), tasks)
        self.assertEqual(len(rows), 102)
        groups = list(pair_slots(rows))
        self.assertEqual(Counter(map(len, groups)), {len(PRIMARY): 18, len(CONTROLS): 6})
        self.assertTrue(all(len({row["cache_start"] for row in group}) == 1 for group in groups))
        chart_rows = [
            {**row, "task": row["task_id"], "state": "complete"}
            for row in rows
            if row["arm"] in PRIMARY
        ]
        self.assertEqual(paired_orders(chart_rows, PRIMARY), {row["order"] for row in chart_rows})
        first = chart_rows[0]
        chart_rows[0] = {**first, "state": "pending"}
        excluded = {
            row["order"]
            for row in chart_rows
            if (row["task"], row["repeat"], row["cache_start"])
            == (first["task"], first["repeat"], first["cache_start"])
        }
        self.assertEqual(
            paired_orders(chart_rows, PRIMARY), {r["order"] for r in chart_rows} - excluded
        )
        with self.assertRaisesRegex(ValueError, "consecutive"):
            validate_schedule([rows[0], rows[3], rows[2], rows[1], *rows[4:]])

    def test_memory_cannot_be_silently_enabled_or_aliased_to_a_tier(self):
        rows = schedule(["a", "b", "c"], seed=741)
        for arm, field, wrong in (
            ("loom-treesitter", "memory_enabled", True),
            ("loom-scip-memory", "memory_enabled", False),
            ("loom-scip-memory", "index_tier", "scip-memory"),
            ("baseline", "index_tier", "treesitter"),
        ):
            with self.subTest(arm=arm, field=field):
                altered = copy.deepcopy(rows)
                next(row for row in altered if row["arm"] == arm)[field] = wrong
                with self.assertRaisesRegex(ValueError, "identity"):
                    validate_schedule(altered)

    def test_missing_and_duplicate_attempts_refuse(self):
        rows = schedule(["a", "b", "c"], seed=741)
        for altered in (rows[:-1], rows + [rows[0]]):
            with self.assertRaisesRegex(ValueError, "every scenario"):
                validate_schedule(altered)

    def test_memory_is_paired_with_its_own_compiler_and_baseline(self):
        pairs = set(contrasts())
        for tier in ("treesitter", "scip"):
            self.assertIn((f"loom-{tier}-memory", f"loom-{tier}"), pairs)
            self.assertIn((f"loom-{tier}-memory", "baseline"), pairs)
