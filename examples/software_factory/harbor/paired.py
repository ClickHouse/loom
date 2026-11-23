"""Compare matched factory attempts from a frozen schedule."""

from __future__ import annotations

import math
from collections import Counter
from statistics import fmean

from bench.bootstrap import paired_bootstrap
from examples.software_factory.factory.model import digest

from .arms import ALL, CACHE_STARTS, EFFICIENCY_TARGET, PRIMARY, contrasts, validate_schedule
from .cache_start import pair_usage
from .cohorts import comparison_cohorts

ARMS = tuple(ALL)
EFFICIENCY_METRICS = ("reported_provider_cost_usd", "agent_time_s", "end_to_end_s")
MEMORY_PEERS = {
    "loom-treesitter-memory": "loom-treesitter",
    "loom-scip-memory": "loom-scip",
}


def analyze_cohorts(protocol, entries):
    cohorts = comparison_cohorts(protocol)
    reports = {}
    for family, cohort in cohorts["groups"].items():
        slots = [s for s in protocol["schedule"] if s["task_id"] in cohort["task_ids"]]
        orders = {slot["order"]: index for index, slot in enumerate(slots)}
        rows = [{**slot, "order": orders[slot["order"]]} for slot in slots]
        selected = [
            {**entry, "order": orders[entry["order"]]}
            for entry in entries
            if entry["order"] in orders
        ]
        reports[family] = {
            **analyze({"attempts": rows, "schedule_sha256": digest(rows)}, selected),
            "label": cohort["label"],
            "original_orders": [slot["order"] for slot in slots],
        }
    return {"primary_cohort": cohorts["default"], "pooled_headline": False, "cohorts": reports}


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
            "loom_lesson_chars_main",
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
        "blind_fixed": int(result["judge_fixed"])
        if type(result.get("judge_fixed")) is bool
        else None,
        "reported_provider_cost_usd": _number(result.get("total_provider_cost_usd"))
        if result.get("generation_cost_source") == "claude-code-reported"
        else None,
        "full_suite_regressions": _number(result.get("full_suite_regressions"), integer=True),
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
    if len(tasks) < 2:
        return {
            "estimate": fmean(values),
            "ci95": None,
            "reason": "one scenario is descriptive only",
        }
    result = paired_bootstrap(values, clusters=tasks, seed=seed, resamples=resamples)
    return {"estimate": result.mean, "ci95": [result.lb, result.ub]}


def _target_margin(task_pairs, tasks, *, seed, resamples):
    # Equal-weight task means make the ratio and the paired margin use the same denominator.
    units = [
        (fmean(a for a, _ in pairs), fmean(b for _, b in pairs))
        if all(a is not None and b is not None for a, b in pairs)
        else (None, None)
        for pairs in task_pairs
    ]
    if any(a is None or b is None for a, b in units) or not any(b > 0 for _, b in units):
        return {
            "relative_reduction": None,
            "observed_met": None,
            "lower_bound_met": None,
            "margin": {"estimate": None, "ci95": None},
        }
    margin = _interval(
        [(1 - EFFICIENCY_TARGET) * baseline - actual for actual, baseline in units],
        tasks,
        seed=seed,
        resamples=resamples,
    )
    relative = 1 - fmean(a for a, _ in units) / fmean(b for _, b in units)
    return {
        "relative_reduction": relative,
        "observed_met": _clears_margin(margin["estimate"]),
        "lower_bound_met": _clears_margin(margin["ci95"][0])
        if margin["ci95"] is not None
        else None,
        "margin": margin,
    }


def _clears_margin(value):
    return value >= 0 or math.isclose(value, 0, abs_tol=1e-12)


def _efficiency_reached(values):
    if False in values:
        return False
    return True if all(value is True for value in values) else None


def _threshold_summary(verified):
    return {
        "relative_reduction_target": EFFICIENCY_TARGET,
        "cache_and_verifier_verified": verified,
        "interpretation": (
            "Observed means and issue-clustered intervals describe this cohort. "
            "The pilot has no interval. An infrastructure or cache mismatch withholds the result."
        ),
        "cache_starts": {
            state: {"efficiency_vs_no_loom": {}, "memory_vs_code_index": {}}
            for state in CACHE_STARTS
        },
    }


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
        if (
            type(result.get("memory_enabled")) is not bool
            or any(
                result.get(k) != row[k]
                for k in ("task_id", "mode", "arm", "index_tier", "memory_enabled")
            )
            or result.get("cache_start") != row.get("cache_start")
        ):
            raise ValueError("Attempt identity differs from its schedule slot")
        identity.add((result["provider"], result["kind"]))
        value = _metrics(result)
        if not row["memory_enabled"] and value["memory_rows_written"] != 0:
            raise ValueError("Memory-disabled treatment has nonzero or unverified writes")
        if row["mode"] == "baseline" and (
            value["model_selected_loom_requests"] not in (None, 0)
            or any(value[key] != 0 for key in ("loom_logical_requests", "loom_physical_requests"))
        ):
            raise ValueError("Baseline Loom exposure is nonzero or unverified")
        observed[order], metrics[order] = result, value
    if len(identity) > 1:
        raise ValueError("Provider or task kind changed within the controlled cohort")
    missing = sorted(set(range(len(rows))) - set(observed))
    cache_pairs = "cache_start" in rows[0]
    pilot = rows[0].get("pilot") is True
    mode = "pilot" if pilot else "full"
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
        "study_mode": f"cache-{mode}" if cache_pairs else mode,
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
    if cache_pairs:
        report["cache_start_deltas"] = {state: {} for state in CACHE_STARTS}
        if not pilot:
            report["cache_start_aa_reference"] = {state: {} for state in CACHE_STARTS}
        report["cache_starts_verified"] = (
            not missing
            and all(result.get("cache_start_verified") is True for result in observed.values())
            and all(
                pair["first_prefix_token_count_match"] is True
                for pair in pair_usage(rows, observed)
            )
        )
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
    if cache_pairs:
        verified = report["cache_starts_verified"] and all(
            result.get("harbor_reward_valid") is True for result in observed.values()
        )
        report["cache_start_targets"] = _threshold_summary(verified)
    if missing:
        return report  # Missing attempts are visible; no partial-cohort selection.
    lookup = {
        (r["task_id"], r["repeat"], r["arm"], r.get("cache_start")): metrics[r["order"]]
        for r in rows
    }
    comparisons = []
    for i, state in enumerate(CACHE_STARTS if cache_pairs else (None,)):
        repeats = [i] if pilot else range(3)
        deltas = report["paired_deltas"] if state is None else report["cache_start_deltas"][state]
        comparisons.extend((deltas, left, right, repeats, state) for left, right in contrasts())
        if not pilot:
            aa = (
                report["aa_reference"]
                if state is None
                else report["cache_start_aa_reference"][state]
            )
            comparisons.append((aa, "baseline-b", "baseline-a", ["aa"], state))
    for destination, left, right, repeats, state in comparisons:
        contrast, efficiency = {}, {}
        qualifies = (
            state is not None
            and right == "baseline"
            and left in PRIMARY
            and report["cache_start_targets"]["cache_and_verifier_verified"]
        )
        for key in next(iter(metrics.values())):
            task_pairs = [
                [
                    (
                        lookup[task, repeat, left, state][key],
                        lookup[task, repeat, right, state][key],
                    )
                    for repeat in repeats
                ]
                for task in tasks
            ]
            differences = [
                None
                if any(a is None or b is None for a, b in pairs)
                else fmean(a - b for a, b in pairs)
                for pairs in task_pairs
            ]
            contrast[key] = {
                "task_deltas": dict(zip(tasks, differences, strict=True)),
                **_interval(differences, tasks, seed=seed, resamples=resamples),
            }
            if qualifies and key in EFFICIENCY_METRICS:
                efficiency[key] = _target_margin(task_pairs, tasks, seed=seed, resamples=resamples)
        destination[left + "-minus-" + right] = contrast
        if state is None or not report["cache_start_targets"]["cache_and_verifier_verified"]:
            continue
        targets = report["cache_start_targets"]["cache_starts"][state]
        if efficiency:
            targets["efficiency_vs_no_loom"][left] = {
                **efficiency,
                "hidden_test_pass_rate_delta": contrast["quality"],
                "all_observed_met": _efficiency_reached(
                    [value["observed_met"] for value in efficiency.values()]
                ),
                "all_lower_bounds_met": _efficiency_reached(
                    [value["lower_bound_met"] for value in efficiency.values()]
                ),
            }
        if MEMORY_PEERS.get(left) == right:
            hidden = contrast["quality"]
            lesson_chars = [
                lookup[task, repeat, left, state]["loom_lesson_chars_main"]
                for task in tasks
                for repeat in repeats
            ]
            targets["memory_vs_code_index"][left] = {
                "code_only_arm": right,
                "main_session_linked_lesson_attempts": sum(
                    chars is not None and chars > 0 for chars in lesson_chars
                ),
                "main_session_lesson_evidence_missing": lesson_chars.count(None),
                "hidden_test_pass_rate_delta": hidden,
                "blind_fixed_rate_delta": contrast["blind_fixed"],
                "regressions_per_attempt_delta": contrast["full_suite_regressions"],
                "observed_hidden_test_lift": hidden["estimate"] > 0,
                "hidden_test_lift_lower_bound": hidden["ci95"][0] > 0 if hidden["ci95"] else None,
            }
    return report
