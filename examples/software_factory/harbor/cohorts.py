"""Keep historical issues and authored experiments in separate comparisons."""

from __future__ import annotations

import json
from pathlib import Path


def comparison_cohorts(protocol):
    tasks = {slot["task_id"] for slot in protocol["schedule"]}
    families = {}
    manifest = protocol.get("historical_manifest")
    if manifest:
        for row in json.loads(Path(manifest).read_text())["tasks"]:
            if row["task_id"] in tasks and row.get("scenario_family"):
                families[row["task_id"]] = row["scenario_family"]
    for task, config in protocol.get("tasks", {}).items():
        family = config.get("scenario_family")
        if task not in tasks or not family:
            continue
        if task in families and families[task] != family:
            raise ValueError("Task family differs between frozen manifests")
        families[task] = family
    groups = {}
    for task in sorted(tasks):
        groups.setdefault(families.get(task, "unclassified"), []).append(task)
    default = "historical-issue" if "historical-issue" in groups else sorted(groups)[0]
    labels = {
        "historical-issue": "Historical repository issues (primary)",
        "authored-simulated-memory-history": "Authored memory scenarios (separate experiment)",
        "unclassified": "Unclassified scenarios",
    }
    return {
        "default": default,
        "pooled_headline": False,
        "groups": {
            family: {"label": labels.get(family, family), "task_ids": ids}
            for family, ids in sorted(groups.items())
        },
    }
