"""Exploratory analysis of the persisted pilot order; never authorize inference."""

from __future__ import annotations

import math
from collections import Counter
from statistics import fmean

from bench.bootstrap import paired_bootstrap

from .model import digest

ARMS = ("baseline", "orchestrated", "natural", "baseline-a", "baseline-b")


def _number(value, *, integer=False):
    if value is None:
        return None
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("Metrics must be finite nonnegative numbers or explicit unknowns")
    return value


def _metrics(result: dict) -> dict:
    if type(result["resolved"]) is not bool:
        raise ValueError("Resolution must be an objective boolean")
    counts = {
        key: _number(result.get(key), integer=True)
        for key in (
            "tool_total_requests",
            "host_loom_requests",
            "model_selected_loom_requests",
            "provider_logical_requests",
            "provider_physical_requests",
            "loom_logical_requests",
            "loom_physical_requests",
            "provider_failed_requests",
        )
    }
    tools, host = counts["tool_total_requests"], counts["host_loom_requests"]
    metrics = {
        **counts,
        "quality": int(result["resolved"]),
        "attempt_duration_s": _number(result.get("duration_s")),
        "end_to_end_s": _number(result.get("end_to_end_s")),
        "total_tool_requests": tools + host if tools is not None and host is not None else None,
        "total_cost_usd": _number(result.get("total_cost_usd"))
        if result.get("cost_status") in {"priced", "offline"}
        else None,
    }
    usage = result.get("provider_usage")
    for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
        complete = (
            isinstance(usage, list)
            and counts["provider_failed_requests"] == 0
            and len(usage) == counts["provider_logical_requests"]
            and all(isinstance(row, dict) and key in row for row in usage)
        )
        values = [_number(row[key], integer=True) for row in usage] if complete else [None]
        metrics["provider_" + key] = sum(values) if None not in values else None
    return metrics


def _interval(values: list, tasks: list[str], *, seed: int, resamples: int) -> dict:
    if None in values:
        return {"estimate": None, "ci95": None, "reason": "unknown metric retained"}
    result = paired_bootstrap(values, clusters=tasks, seed=seed, resamples=resamples)
    return {"estimate": result.mean, "ci95": [result.lb, result.ub]}


def analyze(schedule: dict, entries: list[dict], *, seed=741, resamples=2000) -> dict:
    """Use every scheduled attempt, equal task weights and task-level resampling.

    The input is a trusted controller export, never a candidate-authored grade.
    A caller's live label does not establish provenance or complete billing.
    """
    rows = schedule["attempts"]
    if digest(rows) != schedule["schedule_sha256"]:
        raise ValueError("Persisted schedule hash differs")
    tasks = sorted({row["task_id"] for row in rows})
    expected = {
        (task, repeat, arm) for task in tasks for repeat in range(3) for arm in ARMS[:3]
    } | {(task, "aa", arm) for task in tasks for arm in ARMS[3:]}
    actual = [(r["task_id"], r["repeat"], r["arm"]) for r in rows]
    if (
        len(tasks) != 3
        or len(rows) != 33
        or set(actual) != expected
        or any(type(r["order"]) is not int for r in rows)
        or any(type(r["repeat"]) is not int for r in rows if r["arm"] in ARMS[:3])
        or [r["order"] for r in rows] != list(range(33))
        or any(
            r["mode"] != ("baseline" if r["arm"].startswith("baseline") else r["arm"]) for r in rows
        )
    ):
        raise ValueError("Expected the frozen three-task, three-repeat pilot and A/A pairs")
    if type(resamples) is not int or not 100 <= resamples <= 10000 or type(seed) is not int:
        raise ValueError("Freeze an integer seed and 100-10000 exploratory resamples")
    observed, identity, metrics = {}, set(), {}
    for entry in entries:
        order, result = entry["order"], entry["result"]
        if type(order) is not int or not 0 <= order < len(rows) or order in observed:
            raise ValueError("Duplicate or unscheduled attempt; no best-of selection")
        row = rows[order]
        if result["task_id"] != row["task_id"] or result["mode"] != row["mode"]:
            raise ValueError("Attempt identity differs from its schedule slot")
        identity.add((result["provider"], result["kind"]))
        value = _metrics(result)
        if row["mode"] == "baseline" and any(
            value[key] != 0
            for key in (
                "host_loom_requests",
                "model_selected_loom_requests",
                "loom_logical_requests",
                "loom_physical_requests",
            )
        ):
            raise ValueError("Baseline Loom exposure is nonzero or unverified")
        observed[order], metrics[order] = result, value
    if len(identity) > 1:
        raise ValueError("Provider or task kind changed within the controlled cohort")
    missing = sorted(set(range(len(rows))) - set(observed))
    report = {
        "schema_version": 1,
        "schedule_sha256": digest(rows),
        "results_sha256": digest(entries),
        "complete": not missing,
        "missing_orders": missing,
        "benchmark_claims": False,
        "provenance_verified": False,
        "declared_input": dict(
            zip(("provider_label", "task_kind"), next(iter(identity)), strict=True)
        )
        if identity
        else None,
        "exploratory_only": True,
        "cluster_count": len(tasks),
        "seed": seed,
        "resamples": resamples,
        "paired_deltas": {},
        "aa_reference": {},
        "arms": {},
        "interpretation": (
            "Three task clusters cannot establish significance, savings or adoption. "
            "Input provenance and billing require independent qualification."
        ),
    }
    for arm in ARMS:
        slots = [r["order"] for r in rows if r["arm"] == arm]
        present = [i for i in slots if i in observed]
        costs = [metrics[i]["total_cost_usd"] for i in present]
        priced = len(present) == len(slots) and None not in costs
        successes = sum(metrics[i]["quality"] for i in present)
        total = math.fsum(costs) if priced else None
        report["arms"][arm] = {
            "expected": len(slots),
            "observed": len(present),
            "resolved": successes,
            "statuses": dict(Counter(observed[i]["status"] for i in present)),
            "total_cost_usd": total,
            "cost_per_success_usd": total / successes if total is not None and successes else None,
            "attempts": [{"order": i, **metrics[i]} for i in present],
        }
    costs = [value["total_cost_usd"] for value in metrics.values()]
    report["cohort_total_cost_usd"] = (
        math.fsum(costs) if not missing and None not in costs else None
    )
    if missing:
        return report  # Missing attempts are visible; no partial-cohort selection.
    lookup = {(r["task_id"], r["repeat"], r["arm"]): metrics[r["order"]] for r in rows}
    for destination, left, right, repeats in (
        (report["paired_deltas"], "orchestrated", "baseline", range(3)),
        (report["paired_deltas"], "natural", "baseline", range(3)),
        (report["aa_reference"], "baseline-b", "baseline-a", ["aa"]),
    ):
        contrast = {}
        for key in next(iter(metrics.values())):
            differences = []
            for task in tasks:
                pairs = [
                    (lookup[task, repeat, left][key], lookup[task, repeat, right][key])
                    for repeat in repeats
                ]
                differences.append(
                    None
                    if any(a is None or b is None for a, b in pairs)
                    else fmean(a - b for a, b in pairs)
                )
            contrast[key] = {
                "task_deltas": dict(zip(tasks, differences, strict=True)),
                **_interval(differences, tasks, seed=seed, resamples=resamples),
            }
        destination[left + "-minus-" + right] = contrast
    return report
