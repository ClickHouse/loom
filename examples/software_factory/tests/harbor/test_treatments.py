"""The scheduled memory treatment cannot be confused with code-only retrieval."""

import copy
import unittest
from collections import Counter

from examples.software_factory.harbor.arms import PRIMARY, contrasts, validate_schedule
from examples.software_factory.harbor.live_support import schedule


class TreatmentContracts(unittest.TestCase):
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
