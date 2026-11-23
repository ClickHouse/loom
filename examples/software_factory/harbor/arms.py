"""Treatment identities shared by execution, audits and presentation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Arm:
    index_tier: str | None
    history_seeded: bool
    label: str

    @property
    def memory_enabled(self):
        return self.history_seeded

    @property
    def mode(self):
        return "natural" if self.index_tier else "baseline"

    def identity(self):
        return {
            "mode": self.mode,
            "index_tier": self.index_tier,
            "memory_enabled": self.memory_enabled,
        }


PRIMARY = {
    "baseline": Arm(None, False, "No Loom"),
    "loom-treesitter": Arm("treesitter", False, "Loom + tree-sitter"),
    "loom-treesitter-memory": Arm("treesitter", True, "Loom + tree-sitter + historical memory"),
    "loom-scip": Arm("scip", False, "Loom + SCIP"),
    "loom-scip-memory": Arm("scip", True, "Loom + SCIP + historical memory"),
}
CONTROLS = {name: PRIMARY["baseline"] for name in ("baseline-a", "baseline-b")}
ALL = {**PRIMARY, **CONTROLS}
CACHE_STARTS = ("cold", "warm")


def with_cache_pairs(rows):
    return [
        {
            **row,
            "order": len(CACHE_STARTS) * order + start,
            "repeat": start if row.get("pilot") else row["repeat"],
            "cache_start": state,
        }
        for order, row in enumerate(rows)
        for start, state in enumerate(CACHE_STARTS)
    ]


def validate_schedule(rows):
    """Reject missing arms, repetitions, changed treatments and position imbalance."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Freeze a nonempty five-arm schedule")
    tasks = sorted({row["task_id"] for row in rows})
    pilot = any("pilot" in row for row in rows)
    if pilot and (len(tasks) != 1 or any(row.get("pilot") is not True for row in rows)):
        raise ValueError("A pilot requires one scenario and an explicit marker on every arm")
    if any("cache_start" in row for row in rows):
        return _validate_cache_pairs(rows)
    repeats = range(1 if pilot else 3)
    expected = {(task, repeat, arm) for task in tasks for repeat in repeats for arm in PRIMARY}
    if not pilot:
        expected |= {(task, "aa", arm) for task in tasks for arm in CONTROLS}
    actual = [(row["task_id"], row["repeat"], row["arm"]) for row in rows]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Keep every scenario, repeat and arm in the evaluation design")
    for order, row in enumerate(rows):
        if (
            type(row["order"]) is not int
            or type(row.get("memory_enabled")) is not bool
            or row["order"] != order
            or (row["arm"] in PRIMARY and type(row["repeat"]) is not int)
            or any(row.get(key) != value for key, value in ALL[row["arm"]].identity().items())
        ):
            raise ValueError("Scheduled treatment identity or order differs")
    if pilot:
        return tasks
    global_positions = {arm: Counter() for arm in PRIMARY}
    for task in tasks:
        positions = {arm: Counter() for arm in PRIMARY}
        for repeat in range(3):
            block = [row for row in rows if row["task_id"] == task and row["repeat"] == repeat]
            start = block[0]["order"]
            if [row["order"] for row in block] != list(range(start, start + len(PRIMARY))):
                raise ValueError("Primary arms require contiguous five-arm blocks")
            for position, row in enumerate(block):
                positions[row["arm"]][position] += 1
                global_positions[row["arm"]][position] += 1
        if any(max(counts.values()) != 1 for counts in positions.values()):
            raise ValueError("Each arm must occupy three distinct positions per scenario")
    for counts in global_positions.values():
        values = [counts[position] for position in range(len(PRIMARY))]
        if max(values) - min(values) > 1:
            raise ValueError("Global treatment positions are imbalanced")
    return tasks


def _validate_cache_pairs(rows):
    logical = [
        {**{key: value for key, value in row.items() if key != "cache_start"}, "order": order}
        for order, row in enumerate(rows[::2])
    ]
    tasks = validate_schedule(logical)
    expected = with_cache_pairs(logical)
    if (
        rows != expected
        or any(type(row["order"]) is not int for row in rows)
        or any(type(row.get("memory_enabled")) is not bool for row in rows)
        or any(row["arm"] in PRIMARY and type(row["repeat"]) is not int for row in rows)
    ):
        raise ValueError("Schedule a consecutive cold and warm attempt for each treatment")
    return tasks


def contrasts():
    """Keep memory's incremental effect separate from the code-index effect."""
    return (
        *((name, "baseline") for name in PRIMARY if name != "baseline"),
        ("loom-treesitter-memory", "loom-treesitter"),
        ("loom-scip-memory", "loom-scip"),
        ("loom-scip", "loom-treesitter"),
        ("loom-scip-memory", "loom-treesitter-memory"),
    )
