"""Treatment identities shared by execution, audits and presentation."""

from __future__ import annotations

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
EFFICIENCY_TARGET = 0.20


def validate_schedule(rows):
    """Reject incomplete rounds, reordered arm batches and changed treatments."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Freeze a nonempty five-arm schedule")
    tasks = sorted({row["task_id"] for row in rows})
    pilot = any("pilot" in row for row in rows)
    if pilot and (len(tasks) != 1 or any(row.get("pilot") is not True for row in rows)):
        raise ValueError("A pilot requires one scenario and an explicit marker on every arm")
    repeats = range(1 if pilot and len(rows) == len(PRIMARY) else 3)
    expected = {(task, repeat, arm) for task in tasks for repeat in repeats for arm in PRIMARY}
    actual = [(row["task_id"], row["repeat"], row["arm"]) for row in rows]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Keep every scenario, repeat and arm in the evaluation design")
    for order, row in enumerate(rows):
        if (
            type(row["order"]) is not int
            or type(row.get("memory_enabled")) is not bool
            or row["order"] != order
            or type(row["repeat"]) is not int
            or any(row.get(key) != value for key, value in PRIMARY[row["arm"]].identity().items())
        ):
            raise ValueError("Scheduled treatment identity or order differs")
    batch_size = len(PRIMARY)
    task_order = [row["task_id"] for row in rows[: len(tasks) * batch_size : batch_size]]
    ordered = [(task, repeat, arm) for repeat in repeats for task in task_order for arm in PRIMARY]
    if set(task_order) != set(tasks) or actual != ordered:
        raise ValueError("Every round must follow the same five-arm order for each scenario")
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
