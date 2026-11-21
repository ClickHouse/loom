"""Exploratory analysis of the persisted pilot order; never authorize inference."""

from __future__ import annotations

import math
from collections import Counter
from statistics import fmean

from bench.bootstrap import paired_bootstrap
from examples.software_factory.factory.model import digest

from .arms import ALL, contrasts, validate_schedule

ARMS = tuple(ALL)


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
            "agent_test_runs",
            "agent_test_requests",
            "automatic_final_test_runs",
            "loom_tool_calls",
            "memory_rows_written",
            "model_selected_loom_requests",
            "provider_logical_requests",
            "provider_physical_requests",
            "loom_logical_requests",
            "loom_physical_requests",
            "provider_failed_requests",
        )
    }
    tools = counts["tool_total_requests"]
    metrics = {
        **counts,
        "quality": int(result["resolved"]),
        "attempt_duration_s": _number(result.get("duration_s")),
        "agent_time_s": _number(result.get("agent_time_s")),
        "end_to_end_s": _number(result.get("end_to_end_s")),
        "generation_cost_usd": _number(result.get("generation_cost_usd")),
        "total_provider_cost_usd": _number(result.get("total_provider_cost_usd")),
        "total_tool_requests": tools,
        "total_cost_usd": _number(result.get("total_cost_usd"))
        if result.get("cost_status") in {"priced", "offline"}
        else None,
    }
    usage = result.get("provider_usage")
    for key in (
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
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
    tasks = validate_schedule(rows)
    if type(resamples) is not int or not 100 <= resamples <= 10000 or type(seed) is not int:
        raise ValueError("Freeze an integer seed and 100-10000 exploratory resamples")
    observed, identity, metrics = {}, set(), {}
    for entry in entries:
        order, result = entry["order"], entry["result"]
        if type(order) is not int or not 0 <= order < len(rows) or order in observed:
            raise ValueError("Duplicate or unscheduled attempt; no best-of selection")
        row = rows[order]
        if type(result.get("memory_enabled")) is not bool or any(
            result.get(k) != row[k]
            for k in ("task_id", "mode", "arm", "index_tier", "memory_enabled")
        ):
            raise ValueError("Attempt identity differs from its schedule slot")
        identity.add((result["provider"], result["kind"]))
        value = _metrics(result)
        if not row["memory_enabled"] and value["memory_rows_written"] != 0:
            raise ValueError("Memory-disabled treatment has nonzero or unverified writes")
        if row["mode"] == "baseline" and any(
            value[key] != 0
            for key in (
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
            "Intervals resample complete scenario clusters, not correlated stages or repeats. "
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
            **ALL[arm].identity(),
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
    comparisons = [
        (report["paired_deltas"], left, right, range(3)) for left, right in contrasts()
    ] + [(report["aa_reference"], "baseline-b", "baseline-a", ["aa"])]
    for destination, left, right, repeats in comparisons:
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
