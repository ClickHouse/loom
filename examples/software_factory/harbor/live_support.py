"""Build the balanced schedule shared by native execution and analysis."""

from __future__ import annotations

import random

from .arms import ALL, CONTROLS, PRIMARY


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
    groups, task_order, arms = [], sorted(task_ids), list(PRIMARY)
    rng.shuffle(task_order)
    rng.shuffle(arms)
    if task_families is not None:
        if set(task_families) != set(task_ids) or any(
            not isinstance(family, str) or not family for family in task_families.values()
        ):
            raise ValueError("Declare every task family before scheduling")
        families = list(dict.fromkeys(task_families[task] for task in task_order))
        positions = [
            task for family in families for task in task_order if task_families[task] == family
        ]
    else:
        positions = task_order
    ordinals = {task: index for index, task in enumerate(positions)}
    for task in task_order:
        for repeat in range(repeats):
            # Three repeats cannot occupy all five positions. Consecutive rotations
            # minimize the position imbalance across the entire frozen scenario set.
            offset = (ordinals[task] * repeats + repeat) % len(arms)
            groups.append((task, repeat, arms[offset:] + arms[:offset]))
        aa = list(CONTROLS)
        rng.shuffle(aa)
        groups.append((task, "aa", aa))
    rng.shuffle(groups)
    for task, repeat, arms in groups:
        for arm in arms:
            rows.append(
                {
                    "order": len(rows),
                    "task_id": task,
                    "repeat": repeat,
                    "arm": arm,
                    **ALL[arm].identity(),
                }
            )
    return rows
