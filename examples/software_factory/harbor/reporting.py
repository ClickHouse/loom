"""Incremental study reports and derived native Harbor views; no executions."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import fmean
from urllib.parse import quote, urlencode
from uuid import UUID, uuid5

from .arms import PRIMARY
from .cohorts import comparison_cohorts

PRIMARY_VIEWS = {name: "no-loom" if name == "baseline" else name for name in PRIMARY}
VIEW_KIND = "factory-derived-harbor-comparison"
NORMALIZED_FIELDS = [
    "result.task_name",
    "result.task_checksum",
    "lock.task.name",
    "lock.task.digest",
]


def _read(path):
    return json.loads(Path(path).read_text())


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    )
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix="." + path.name, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _tree_hashes(folder):
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise PermissionError("Reporting refuses symlinked trial assets")
        if path.is_file():
            result[str(path.relative_to(folder))] = _hash(path)
    return result


def _groups(schedule):
    groups = {arm: [s for s in schedule if s["arm"] == arm] for arm in PRIMARY_VIEWS}
    unknown = {s["arm"] for s in schedule} - set(PRIMARY_VIEWS) - {"baseline-a", "baseline-b"}
    if unknown:
        raise ValueError("Schedule contains an unsupported comparison arm")
    controls = [s for s in schedule if s["arm"] in {"baseline-a", "baseline-b"}]
    if controls:
        groups["aa-controls"] = controls
    return groups


def _settings(protocol):
    output = Path(protocol["output_root"]).resolve()
    settings = protocol.get("reporting", {})
    jobs = Path(settings.get("viewer_jobs_root", output / "jobs")).expanduser().resolve()
    prefix = settings.get("job_prefix")
    if prefix is None:
        prefix = (
            "" if jobs == output / "jobs" else output.name + "-" + _digest(str(output))[:8] + "-"
        )
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_.-]*", prefix):
        raise ValueError(
            "Report job_prefix must contain only letters, numbers, dots, dashes or underscores"
        )
    base_url = settings.get("viewer_base_url", "http://127.0.0.1:8080").rstrip("/")
    names = {arm: prefix + name for arm, name in PRIMARY_VIEWS.items()}
    names["aa-controls"] = prefix + "aa-controls"
    return output, jobs, base_url, names


def _pair_identity(protocol, slot):
    task_id = slot["task_id"]
    public = next(
        row for row in _read(protocol["historical_manifest"])["tasks"] if row["task_id"] == task_id
    )
    spec = protocol["harbor_specs"][task_id]
    identity = {
        "kind": "paired-primary-presentation-identity",
        "task_id": task_id,
        "repeat_index": slot["repeat"],
        "base_commit": public["base_commit"],
        "source_sha256": spec["source_sha256"],
        "grader_sha256": spec["grader_sha256"],
    }
    return f"{task_id}--repeat-{slot['repeat'] + 1}", _digest(identity), identity


def _copy_trial(source, destination, raw, slot, protocol):
    from harbor.models.job.lock import TrialLock
    from harbor.models.trial.result import TrialResult

    source_hash = _hash(source / "result.json")
    source_lock = _read(source / "lock.json")
    derived, lock = copy.deepcopy(raw), copy.deepcopy(source_lock)
    identity = None
    if slot["arm"] in PRIMARY_VIEWS:
        name, checksum, identity = _pair_identity(protocol, slot)
        derived.update(task_name=name, task_checksum=checksum)
        lock["task"].update(name=name, digest="sha256:" + checksum)
    TrialResult.model_validate(derived)
    TrialLock.model_validate(lock)
    provenance = {
        "kind": VIEW_KIND,
        "not_new_execution": True,
        "order": slot["order"],
        "arm": slot["arm"],
        "task_id": slot["task_id"],
        "repeat": slot["repeat"],
        "source_trial": str(source),
        "original_trial_id": raw["id"],
        "original_trial_name": raw["trial_name"],
        "original_task_name": raw["task_name"],
        "original_task_checksum": raw["task_checksum"],
        "original_task_id": raw["task_id"],
        "original_task_lock": source_lock["task"],
        "original_result_sha256": source_hash,
        "original_config_sha256": _hash(source / "config.json"),
        "presentation_task_name": derived["task_name"],
        "presentation_checksum": derived["task_checksum"],
        "pair_identity": identity,
        "normalized_fields": NORMALIZED_FIELDS if identity else [],
    }
    marker = destination / "presentation-provenance.json"
    if destination.exists():
        if not marker.exists() or _read(marker).get("original_result_sha256") != source_hash:
            raise PermissionError("Derived trial conflicts with its immutable measured result")
        cached = _read(marker)
        if any(cached.get(key) != value for key, value in provenance.items()):
            raise PermissionError("Derived trial provenance changed")
        if (
            _read(destination / "result.json") != derived
            or _read(destination / "lock.json") != lock
        ):
            raise PermissionError("Derived trial metrics or presentation identity changed")
        if _tree_hashes(source) != cached["source_files"]:
            raise PermissionError("Immutable measured trial assets changed")
        for relative, expected in cached["source_files"].items():
            if relative not in {"result.json", "lock.json"}:
                if (
                    _hash(destination / relative) != expected
                    or _hash(source / relative) != expected
                ):
                    raise PermissionError("Immutable trial asset changed")
        return derived, lock, cached, False
    staging = destination.parent.parent / ".factory-report-staging" / _digest(str(destination))
    staging.parent.mkdir(parents=True, exist_ok=True)
    stage_marker = staging / ".factory-report-stage.json"
    stage_identity = {"kind": VIEW_KIND, "source": str(source), "source_sha256": source_hash}
    if staging.exists():
        if not stage_marker.exists() or _read(stage_marker) != stage_identity:
            raise PermissionError("Unowned reporting staging directory")
        shutil.rmtree(staging)
    staging.mkdir()
    _atomic(stage_marker, stage_identity)
    before = _tree_hashes(source)
    try:
        for item in source.iterdir():
            if item.is_symlink():
                raise PermissionError("Reporting refuses symlinked trial assets")
            if item.is_dir():
                shutil.copytree(item, staging / item.name)
            else:
                shutil.copy2(item, staging / item.name)
        if _tree_hashes(source) != before:
            raise PermissionError("Measured trial changed during reporting")
        copied = _tree_hashes(staging)
        if any(copied.get(path) != value for path, value in before.items()):
            raise PermissionError("Copied trial assets differ from measured assets")
        _atomic(staging / "result.json", derived)
        _atomic(staging / "lock.json", lock)
        provenance["source_files"] = before
        _atomic(staging / "presentation-provenance.json", provenance)
        stage_marker.unlink()
        staging.rename(destination)
    except BaseException:
        # A marked staging directory can be recovered by the next report call.
        raise
    return derived, lock, provenance, True


def _native_views(protocol, observed, groups, jobs, names, job_name):
    from harbor.models.job.config import JobConfig
    from harbor.models.job.lock import JobLock
    from harbor.models.job.result import JobResult, JobStats
    from harbor.models.trial.result import TrialResult

    source = Path(protocol["output_root"]).resolve() / "jobs" / job_name
    if not (source / "result.json").exists():
        return {"status": "awaiting_source_job", "copied_trials": 0, "views": {}}
    source_result = _read(source / "result.json")
    source_config, source_lock = _read(source / "config.json"), _read(source / "lock.json")
    source_id = UUID(source_result["id"])
    jobs.mkdir(parents=True, exist_ok=True)
    copied, views = 0, {}
    for arm, slots in groups.items():
        name = names[arm]
        destination = jobs / name
        owner = {
            "kind": VIEW_KIND,
            "source_job": str(source),
            "source_job_id": str(source_id),
            "arm": arm,
        }
        marker = destination / ".factory-report-view.json"
        if destination.exists():
            if not marker.exists() or _read(marker) != owner:
                raise PermissionError("Refuse to overwrite an unrelated Harbor job")
        else:
            destination.mkdir()
            _atomic(marker, owner)
        results, locks, origins = [], [], []
        for slot in slots:
            entry = observed.get(slot["order"])
            if entry is None:
                continue
            folder = Path(entry["result"]["harbor_trial"]).resolve()
            if not folder.is_relative_to(source):
                raise PermissionError("Trial belongs to a different measured job")
            raw = _read(folder / "result.json")
            if raw["trial_name"] != folder.name or not raw.get("finished_at"):
                raise PermissionError("Only finalized measured trials may enter a view")
            derived, lock, provenance, fresh = _copy_trial(
                folder, destination / folder.name, raw, slot, protocol
            )
            copied += fresh
            results.append(TrialResult.model_validate(derived))
            locks.append(lock)
            origins.append(provenance)
        stats = JobStats.from_trial_results(results, n_total_trials=len(slots))
        for evaluation in stats.evals.values():
            values = [
                reward
                for reward, trials in evaluation.reward_stats.get("reward", {}).items()
                for _ in trials
            ]
            evaluation.metrics = [{"mean": fmean(values)}] if values else []
        all_costs = [r.compute_token_cost_totals()[-1] for r in results]
        if any(cost is None for cost in all_costs):
            stats.cost_usd = None
        complete = len(results) == len(slots)
        started = min(
            (r.started_at for r in results),
            default=datetime.fromisoformat(source_result["started_at"]),
        )
        updated = max((r.finished_at for r in results), default=started)
        aggregate = JobResult(
            id=uuid5(source_id, "derived-view:" + name),
            started_at=started,
            updated_at=updated,
            finished_at=updated if complete else None,
            n_total_trials=len(slots),
            stats=stats,
        )
        config = copy.deepcopy(source_config)
        config["job_name"] = name
        config["tasks"] = [copy.deepcopy(source_config["tasks"][s["order"]]) for s in slots]
        JobConfig.model_validate(config)
        lock = copy.deepcopy(source_lock)
        lock["trials"] = locks
        JobLock.model_validate(lock)
        _atomic(destination / "config.json", config)
        _atomic(destination / "lock.json", lock)
        _atomic(destination / "result.json", aggregate.model_dump(mode="json"))
        info = {
            "job_name": name,
            "derived_job_id": str(aggregate.id),
            "expected": len(slots),
            "observed": len(results),
            "pending": len(slots) - len(results),
            "trials": origins,
        }
        _atomic(destination / "presentation-provenance.json", {**owner, "view": info})
        views[arm] = info
    return {"status": "ready", "copied_trials": copied, "views": views}


def _trace_table(record):
    fields = (
        ("First prompt input", "first_prompt_input_tokens"),
        ("First prompt cache reads", "first_cache_read_tokens"),
        ("First prompt cache writes", "first_cache_write_tokens"),
        ("First prompt uncached input", "first_uncached_input_tokens"),
        ("Solver tokens", "model_total_tokens"),
        ("Solver uncached input", "model_uncached_input_tokens"),
        ("Solver cache reads", "model_cache_read_tokens"),
        ("Solver cache writes", "model_cache_write_tokens"),
        ("Solver five-minute cache writes", "model_cache_write_5m_tokens"),
        ("Solver one-hour cache writes", "model_cache_write_1h_tokens"),
        ("Solver output", "model_output_tokens"),
        ("Loom provider input", "loom_provider_input_tokens"),
        ("Loom provider output", "loom_provider_output_tokens"),
        ("Loom embedding tokens", "loom_embedding_tokens"),
        ("Full-suite failing tests", "full_suite_failed_tests"),
        ("Main-session model responses", "model_turns_main"),
        ("Main-session parallel tool groups", "parallel_tool_groups_main"),
        ("Main-session discovery calls", "tool_discovery_calls_main"),
        ("Native CLI duration (s)", "claude_cli_time_s"),
        ("Native model API duration (s)", "claude_api_time_s"),
        ("Main-session Loom errors", "loom_errors_main"),
        ("Main-session Loom result characters", "loom_result_chars_main"),
        ("Main-session Loom body characters", "loom_source_chars_main"),
        ("Main-session lesson characters", "loom_lesson_chars_main"),
        ("Main-session repeated lesson characters", "loom_repeated_lesson_chars_main"),
    )
    rows = "\n".join(f"| {label} | {_number(record.get(key))} |" for label, key in fields)
    return (
        "\n| Diagnostic | Value |\n| --- | ---: |\n" + rows + "\n\n"
        "API time includes model, network and queuing. Concurrent tools can share one "
        "model response. Character counts are observed content, not provider tokens.\n\n"
    )


def _summary(slots, observed):
    records = [observed[s["order"]]["result"] for s in slots if s["order"] in observed]
    costs = [r.get("generation_cost_usd") for r in records]
    complete = len(records) == len(slots)
    return {
        "expected": len(slots),
        "observed": len(records),
        "pending": len(slots) - len(records),
        "cache_starts_verified": sum(r.get("cache_start_verified") is True for r in records)
        if any("cache_start" in slot for slot in slots)
        else None,
        "resolved": sum(r["resolved"] for r in records),
        "success_rate": sum(r["resolved"] for r in records) / len(slots)
        if complete and slots
        else None,
        "infrastructure_errors": sum(not r.get("harbor_reward_valid", False) for r in records),
        "generation_cost_usd": sum(costs) if costs and None not in costs else None,
        "total_provider_cost_usd": _known_sum(records, "total_provider_cost_usd"),
        "equal_cold_provider_cost_usd": _known_sum(records, "equal_cold_provider_cost_usd"),
        "equal_warm_provider_cost_usd": _known_sum(records, "equal_warm_provider_cost_usd"),
        "loom_provider_cost_usd": _known_sum(records, "loom_provider_cost_usd"),
        "known_generation_cost_usd": sum(c for c in costs if c is not None) if costs else None,
        "unknown_generation_costs": sum(c is None for c in costs),
        "mean_end_to_end_s": fmean(r["end_to_end_s"] for r in records) if records else None,
        "mean_native_duration_s": fmean(r["native_duration_s"] for r in records)
        if records
        else None,
        "selected_loom_calls": _known_sum(records, "model_selected_loom_requests"),
        "agent_time_s": _known_sum(records, "agent_time_s"),
        "agent_test_requests": _known_sum(records, "agent_test_requests"),
        "agent_test_runs": _known_sum(records, "agent_test_runs"),
        "loom_tool_calls": _known_sum(records, "loom_tool_calls"),
        "memory_rows_written": _known_sum(records, "memory_rows_written"),
        "full_suite_regressions": _known_sum(records, "full_suite_regressions"),
        "judge_fixed": _known_sum(records, "judge_fixed"),
        **{
            key: _known_sum(records, key)
            for key in (
                "model_input_tokens",
                "model_output_tokens",
                "model_total_tokens",
                "model_cache_read_tokens",
                "model_cache_write_tokens",
            )
        },
        "unknown_loom_counts": sum(r.get("loom_physical_requests") is None for r in records),
    }


def _known_sum(records, key):
    values = [r.get(key) for r in records]
    return sum(values) if values and None not in values else None


def _cache_arm_summaries(slots, observed):
    return {
        state: {
            arm: _summary(
                [s for s in slots if s.get("cache_start") == state and s["arm"] == arm], observed
            )
            for arm in PRIMARY_VIEWS
        }
        for state in dict.fromkeys(s["cache_start"] for s in slots if "cache_start" in s)
    }


def _number(value, digits=3):
    return "pending / unknown" if value is None else f"{value:.{digits}f}"


def _table(arms, *, cache_starts=False):
    lines = [
        "| Arm | Observed / expected | Pending | Resolved | Solver USD | Observed provider USD "
        "| Equal cold USD | Equal warm USD "
        "| Mean full end-to-end (s) | Mean native Harbor (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if cache_starts:
        lines[0] += " Cache starts verified |"
        lines[1] += "---:|"
    for arm, info in arms.items():
        lines.append(
            f"| {arm} | {info['observed']} / {info['expected']} | {info['pending']} "
            f"| {info['resolved']} | {_number(info['generation_cost_usd'], 7)} "
            f"| {_number(info['total_provider_cost_usd'], 7)} "
            f"| {_number(info['equal_cold_provider_cost_usd'], 7)} "
            f"| {_number(info['equal_warm_provider_cost_usd'], 7)} "
            f"| {_number(info['mean_end_to_end_s'])} "
            f"| {_number(info['mean_native_duration_s'])} |"
        )
        if cache_starts:
            lines[-1] += f" {info['cache_starts_verified']} / {info['observed']} |"
    return "\n".join(lines) + (
        "\n\nEqual-cache estimates reprice the first solver input as a full five-minute "
        "cache write or a full cache read. Later turns and Loom charges use the recorded values.\n"
    )


def _cache_tables(cache_arms):
    return "\n".join(
        f"## Planned {state} start\n\n" + _table(arms, cache_starts=True)
        for state, arms in cache_arms.items()
    )


def generate_reports(protocol, entries, *, job_name="live"):
    """Regenerate aggregates and add each finalized trial's native assets once.

    Protocol `reporting` optionally sets viewer_jobs_root, viewer_base_url and
    job_prefix. Pending cells stay null; derived views are never new executions.
    """
    if job_name != "live":
        return None
    output, jobs, base, names = _settings(protocol)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "reporting.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        return _generate(protocol, entries, output, jobs, base, names, job_name)


def _generate(protocol, entries, output, jobs, base, names, job_name):
    from examples.software_factory.harbor.charts import generate_charts

    schedule = protocol["schedule"]
    slots_by_order = {slot["order"]: slot for slot in schedule}
    if len(slots_by_order) != len(schedule):
        raise ValueError("Duplicate schedule slots")
    observed = {}
    for entry in entries:
        order = entry["order"]
        if order not in slots_by_order or order in observed:
            raise ValueError("Duplicate or unscheduled measured result")
        slot, record = slots_by_order[order], copy.deepcopy(entry["result"])
        if (
            record["task_id"] != slot["task_id"]
            or record["mode"] != slot["mode"]
            or record.get("arm") != slot["arm"]
            or record.get("index_tier") != slot.get("index_tier")
        ):
            raise ValueError("Measured result differs from its scheduled identity")
        raw = _read(Path(record["harbor_trial"]) / "result.json")
        valid = raw["reward_valid"] is True
        resolved = (
            valid and ((raw.get("verifier_result") or {}).get("rewards") or {}).get("reward") == 1
        )
        if record["resolved"] is not resolved or record.get("harbor_reward_valid") is not valid:
            raise ValueError("Study success differs from the immutable native reward")
        start, finish = (datetime.fromisoformat(raw[key]) for key in ("started_at", "finished_at"))
        record["native_duration_s"] = (finish - start).total_seconds()
        observed[order] = {"order": order, "result": record}
    groups = _groups(schedule)
    native = _native_views(protocol, observed, groups, jobs, names, job_name)
    comparison_query = urlencode([("job", names[arm]) for arm in PRIMARY_VIEWS])
    links = {
        "comparison": base + "/compare?" + comparison_query,
        "cost": base + "/compare?" + comparison_query + "&color=cost",
        "native_time": base + "/compare?" + comparison_query + "&color=time",
    }
    views_manifest = {
        "kind": VIEW_KIND,
        "source_job": str(output / "jobs" / job_name),
        "viewer_jobs_root": str(jobs),
        "views": native["views"],
        "links": links,
        "observed_attempts": len(observed),
        "complete": len(observed) == len(schedule),
        "evaluation_complete": len(observed) == len(schedule)
        and (output / "provider-costs.json").exists()
        and (
            not protocol.get("judge", {}).get("enabled")
            or (output / "judgements/summary.json").exists()
        ),
        "report": str(output / "reports" / "comparison.json"),
    }
    charts = generate_charts(protocol, entries, views_manifest)
    links["charts"] = charts["url"] or charts["html"]
    views_manifest["charts"] = charts
    attempts = []
    report_dir = output / "reports"
    for slot in schedule:
        order = slot["order"]
        entry = observed.get(order)
        group = slot["arm"] if slot["arm"] in PRIMARY_VIEWS else "aa-controls"
        item = {**slot, "state": "observed" if entry else "pending", "metrics": None, "links": {}}
        if entry:
            record = entry["result"]
            folder = Path(record["harbor_trial"])
            prefix = (
                base
                + "/api/jobs/"
                + quote(names[group], safe="")
                + "/trials/"
                + quote(folder.name, safe="")
            )
            item["metrics"] = record
            item["links"] = {"native_job": base + "/jobs/" + quote(names[group], safe="")}
            assets = {
                "trajectory": ("agent/trajectory.json", prefix + "/trajectory"),
                "verifier": ("verifier/grade.json", prefix + "/files/verifier/grade.json"),
                "junit": ("verifier/junit.xml", prefix + "/files/verifier/junit.xml"),
                "factory_result": (
                    "agent/factory-result.json",
                    prefix + "/files/agent/factory-result.json",
                ),
            }
            for label, (path, url) in assets.items():
                if (folder / path).exists():
                    item["links"][label] = url
            for label, suffix in (
                ("model usage by turn", "-model-usage"),
                ("Loom retrieval stages", "-code-traces"),
                ("tool response summary", ""),
            ):
                name = f"attempt-{order:02d}{suffix}.json"
                if (output / "diagnostics" / name).is_file():
                    item["links"][label] = "../../diagnostics/" + name
        attempts.append(item)
        path = report_dir / "attempts" / f"attempt-{order:02d}.json"
        if not path.exists() or _read(path) != item or not path.with_suffix(".md").exists():
            _atomic(path, item)
            text = (
                f"# Attempt {order}\n\n{slot['task_id']} · repeat {slot['repeat']} "
                f"· {slot['arm']} · {item['state']}\n\n"
            )
            if entry:
                record = entry["result"]
                if "cache_start" in slot:
                    verification = (
                        "verified" if record.get("cache_start_verified") is True else "unverified"
                    )
                    text += (
                        f"Planned {slot['cache_start']} start. "
                        f"Provider cache state {verification}.\n\n"
                    )
                text += (
                    f"Resolved: {record['resolved']}. "
                    f"Full end-to-end: {_number(record['end_to_end_s'])}s. "
                    f"Native Harbor: {_number(record['native_duration_s'])}s. "
                    f"Solver cost (USD) {_number(record.get('generation_cost_usd'), 7)}. "
                    "Observed provider cost (USD) "
                    f"{_number(record.get('total_provider_cost_usd'), 7)}. "
                    f"Equal cold {_number(record.get('equal_cold_provider_cost_usd'), 7)}. "
                    f"Equal warm {_number(record.get('equal_warm_provider_cost_usd'), 7)}.\n\n"
                )
                text += (
                    " · ".join(f"[{key}]({value})" for key, value in item["links"].items()) + "\n"
                )
                text += _trace_table(record)
            else:
                text += "No measured result yet. Cost, time and success remain unknown.\n"
            _atomic(path.with_suffix(".md"), text)
    all_scenarios = {
        arm: _summary(slots, observed) for arm, slots in groups.items() if arm in PRIMARY_VIEWS
    }
    cohorts = comparison_cohorts(protocol)
    cohort_reports = {}
    for family, cohort in cohorts["groups"].items():
        slots = [s for s in schedule if s["task_id"] in cohort["task_ids"]]
        cohort_reports[family] = {
            **cohort,
            "arms": {
                arm: _summary([s for s in slots if s["arm"] == arm], observed)
                for arm in PRIMARY_VIEWS
            },
            "aa_controls": _summary([s for s in slots if s["arm"] not in PRIMARY_VIEWS], observed),
        }
    primary = cohort_reports[cohorts["default"]]["arms"]
    primary_tasks = set(cohorts["groups"][cohorts["default"]]["task_ids"])
    controls = _summary(groups.get("aa-controls", []), observed)
    control_arms = {
        arm: _summary([s for s in groups.get("aa-controls", []) if s["arm"] == arm], observed)
        for arm in sorted({s["arm"] for s in groups.get("aa-controls", [])})
    }
    control_report = {
        "summary": controls,
        "arms": control_arms,
        "attempts": [a for a in attempts if a["arm"] not in PRIMARY_VIEWS],
    }
    _atomic(report_dir / "aa-controls.json", control_report)
    _atomic(
        report_dir / "AA-CONTROLS.md",
        "# A/A controls\n\nThese controls are excluded from the primary comparison.\n\n"
        + _table(control_arms),
    )
    paired = sorted(
        {
            (s["task_id"], s["repeat"])
            for s in schedule
            if s["arm"] in PRIMARY_VIEWS and s["task_id"] in primary_tasks
        }
    )
    rows = [
        {
            "task_id": task,
            "repeat": repeat,
            "cells": {
                arm: next(
                    (
                        item["metrics"]
                        for item in attempts
                        if item["task_id"] == task
                        and item["repeat"] == repeat
                        and item["arm"] == arm
                    ),
                    None,
                )
                for arm in PRIMARY_VIEWS
            },
        }
        for task, repeat in paired
    ]
    scenarios, scenario_links = {}, []
    for task in sorted({s["task_id"] for s in schedule}):
        selected = [s for s in schedule if s["task_id"] == task]
        info = {
            "task_id": task,
            "arms": {
                arm: _summary([s for s in selected if s["arm"] == arm], observed)
                for arm in PRIMARY_VIEWS
            },
            "aa_controls": _summary(
                [s for s in selected if s["arm"] not in PRIMARY_VIEWS], observed
            ),
            "aa_control_arms": {
                arm: _summary([s for s in selected if s["arm"] == arm], observed)
                for arm in control_arms
            },
            "attempts": [a for a in attempts if a["task_id"] == task],
            "comparison_links": links,
        }
        cache_arms = _cache_arm_summaries(selected, observed)
        if cache_arms:
            info["cache_start_arms"] = cache_arms
        scenarios[task] = info
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", task) + "-" + _digest(task)[:8]
        scenario_links.append((task, "scenarios/" + safe_name + ".md"))
        _atomic(report_dir / "scenarios" / (safe_name + ".json"), info)
        attempt_links = "\n".join(
            f"- [Attempt {a['order']}](../attempts/attempt-{a['order']:02d}.md): "
            f"{a['arm']}, repeat {a['repeat']}, {a['state']}"
            for a in info["attempts"]
        )
        _atomic(
            report_dir / "scenarios" / (safe_name + ".md"),
            f"# Scenario {task}\n\n"
            + (_cache_tables(cache_arms) if cache_arms else _table(info["arms"]))
            + "\n## A/A controls\n\n"
            + _table(info["aa_control_arms"])
            + "\n"
            + " · ".join(f"[{key}]({value})" for key, value in links.items())
            + "\n\n"
            + attempt_links
            + "\n",
        )
    report = {
        "schema_version": 1,
        "not_new_executions": True,
        "model_requests": 0,
        "complete": len(observed) == len(schedule),
        "expected_attempts": len(schedule),
        "observed_attempts": len(observed),
        "pending_orders": sorted(set(slots_by_order) - set(observed)),
        "scenario_count": len(scenarios),
        "expected_primary_pair_rows": len(rows),
        "primary_arms": primary,
        "primary_cohort": cohorts["default"],
        "cohorts": cohort_reports,
        "pooled_headline": False,
        "all_scenarios_arms": all_scenarios,
        "aa_controls": controls,
        "paired_rows": rows,
        "scenarios": scenarios,
        "attempts": attempts,
        "native_views": native,
        "charts": charts,
        "links": links,
        "timing": (
            "Native Harbor duration is finished_at minus started_at. Full study end-to-end "
            "also includes post-trial index audit. Neither is replaced by the other."
        ),
        "cost_basis": protocol.get("cost_basis"),
        "normalization": (
            "Only derived task display names/checksums and task lock names/digests change. "
            "Original UUIDs, config paths, metrics, costs and timestamps remain unchanged."
        ),
    }
    cache_arms = _cache_arm_summaries(schedule, observed)
    if cache_arms:
        report["cache_start_arms"] = cache_arms
    _atomic(report_dir / "comparison.json", report)
    text = (
        f"# Factory comparison\n\n{len(observed)}/{len(schedule)} attempts observed across "
        f"{len(scenarios)} distinct scenarios. {len(rows)} task/repeat rows are expected "
        f"in the primary comparison: {cohorts['groups'][cohorts['default']]['label']}.\n\n"
        "Historical issues and authored experiments are reported separately. "
        "Native Harbor comparison links include all scenarios.\n\n"
    )
    text += (_cache_tables(cache_arms) if cache_arms else _table(primary)) + "\n"
    text += " · ".join(f"[{key}]({value})" for key, value in links.items())
    for family, cohort in cohort_reports.items():
        if family != cohorts["default"]:
            text += "\n\n## " + cohort["label"] + "\n\n" + _table(cohort["arms"])
    text += (
        "\n\nA/A controls are separate: "
        + str(controls["observed"])
        + " / "
        + str(controls["expected"])
        + " observed.\n\n"
        + report["timing"]
        + "\n\nGeneration estimates are not invoices or all-in infrastructure costs. "
        "Pending cells are missing, not zero-cost failures.\n"
    )
    text += "\n" + "\n".join(f"- [{task}]({path})" for task, path in scenario_links)
    text += "\n- [A/A controls](AA-CONTROLS.md)\n"
    _atomic(report_dir / "COMPARISON.md", text)
    _atomic(
        output / "reporting-manifest.json",
        {
            **views_manifest,
            "expected_primary_pair_rows": len(rows),
        },
    )
    return report
