"""Build the fixed arm batches shared by native execution and analysis."""

from __future__ import annotations

import random

from .arms import PRIMARY


def schedule(
    task_ids: list[str], *, seed: int, repeats: int = 3, task_families: dict | None = None
) -> list[dict]:
    if (
        not task_ids
        or any(not isinstance(task, str) or not task for task in task_ids)
        or len(task_ids) != len(set(task_ids))
        or type(repeats) is not int
        or repeats != 3
        or type(seed) is not int
    ):
        raise ValueError("Freeze unique scenario ids, three repeats and an integer seed")
    rng, rows = random.Random(seed), []
    task_order = sorted(task_ids)
    rng.shuffle(task_order)
    if task_families is not None:
        if set(task_families) != set(task_ids) or any(
            not isinstance(family, str) or not family for family in task_families.values()
        ):
            raise ValueError("Declare every task family before scheduling")
        families = list(dict.fromkeys(task_families[task] for task in task_order))
        task_order = [
            task for family in families for task in task_order if task_families[task] == family
        ]
    for repeat in range(repeats):
        for task in task_order:
            for arm, treatment in PRIMARY.items():
                rows.append(
                    {
                        "order": len(rows),
                        "task_id": task,
                        "repeat": repeat,
                        "arm": arm,
                        **treatment.identity(),
                    }
                )
    return rows
