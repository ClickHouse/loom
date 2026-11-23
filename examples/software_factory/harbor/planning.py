"""Freeze shared execution, treatment, source and accounting policy for any repository profile."""

from __future__ import annotations

import copy
from pathlib import Path

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.pricing import cost_basis

from .arms import PRIMARY, validate_schedule
from .charts import METRICS
from .cohorts import comparison_cohorts
from .runtime import task_inputs, write
from .scopes import binding_for, frozen_json, selected_scopes
from .source import check_files, controller_identity


def public_reproduction_base(lock: dict, protocol: dict) -> dict:
    return {
        "schema_version": 1,
        "suite": {key: lock[key] for key in ("id", "repository_url", "languages", "source_roots")},
        "implementation": {
            "repository_url": "https://github.com/ClickHouse/loom",
            "controller_commit": protocol["controller_source_identity"]["commit"],
            "server_commit": protocol["server_commit"],
            "controller_dirty": protocol["controller_source_identity"]["dirty"],
            "server_dirty": protocol["server_source_identity"]["dirty"],
            "controller_files": protocol["controller_source_identity"]["relative_files"],
            "server_files": protocol["server_source_identity"]["relative_files"],
        },
        "model": {
            "id": lock["model_id"],
            "provider": "anthropic-messages",
            "api_version": "2023-06-01",
        },
        "pricing": {
            key: protocol["cost_basis"][key]
            for key in (
                "source",
                "input_usd_per_million",
                "output_usd_per_million",
                "prompts_over_100000_input",
                "prompts_over_100000_output",
            )
        },
    }


def freeze_plan(
    prepared: dict,
    setup: dict,
    output: Path,
    *,
    lock: dict,
    public_metadata_factory,
    inference: dict,
    budget_usd: str | None,
    reporting: dict | None = None,
) -> Path:
    validate_schedule(prepared["schedule"])
    check_files(prepared["public_inputs"])
    check_files(setup["files"])
    runtime = setup["runtime"]
    tasks = {
        task_id: {**config, "indexes": setup["tasks"][task_id]["indexes"]}
        for task_id, config in prepared["tasks"].items()
    }
    plan = {
        "schema_version": 1,
        "controller": "harbor",
        "harbor_version": "0.27.0",
        "model_id": lock["model_id"],
        "provider": "Anthropic Messages API",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "api_version": "2023-06-01",
        "inference": inference,
        "budget_usd": budget_usd,
        "index_tier": "per-arm",
        "suite": {
            key: lock[key]
            for key in (
                "id",
                "title",
                "repository",
                "languages",
                "source_roots",
                "adapter",
            )
        },
        "tasks": tasks,
        "schedule": prepared["schedule"],
        "schedule_sha256": digest(prepared["schedule"]),
        "schedule_seed": lock["schedule_seed"],
        "memory_histories": prepared["memory_histories"],
        "attempt_scopes": setup["attempt_scopes"],
        "historical_manifest": prepared["historical_manifest"],
        "reference_controls_file": prepared["reference_controls_file"],
        "server_container": runtime["server_container"],
        "runtime_network": runtime["network"],
        "loom_url": runtime["loom_url"],
        "server_commit": runtime["server_commit"],
        "server_source_identity": runtime["server_source_identity"],
        "controller_source_identity": controller_identity(),
        "index_certificate_file": setup["index_certificate_file"],
        "index_certificate_sha256": setup["index_certificate_sha256"],
        "index_content_file": setup["index_content_file"],
        "index_content_sha256": setup["index_content_sha256"],
        "public_inputs": {**prepared["public_inputs"], **setup["files"]},
        "attempt_budget": None,
        "visible_test_call_limit": None,
        "max_physical_loom_requests": None,
        "reporting": reporting or {},
        "cost_basis": cost_basis(lock["model_id"]),
        "judge": {
            "enabled": True,
            "model_id": lock["model_id"],
            "seed": 741,
            "output_tokens": 128000,
        },
        "success_target": {
            name: {"quality": 1, "provider_cost_ratio_max": 0.8, "agent_time_ratio_max": 0.8}
            for name in PRIMARY
            if name != "baseline"
        },
        "arms": {name: {"label": arm.label, **arm.identity()} for name, arm in PRIMARY.items()},
        "metrics": [dict(metric) for metric in METRICS],
        "files": {},
    }
    plan["comparison_cohorts"] = comparison_cohorts(plan)
    for slot in plan["schedule"]:
        binding_for(plan, slot)
        task_inputs(plan, slot["task_id"], slot["index_tier"], slot=slot)
    if len(selected_scopes(plan)) != len(tasks) * 2:
        raise PermissionError("Every scenario needs exactly two certified code tiers")
    for task_id, row in plan["memory_histories"].items():
        fixture = frozen_json(row["fixture_file"], row["fixture_sha256"])
        if fixture["case_id"] != task_id:
            raise PermissionError("History fixture and task identities differ")
    common = public_reproduction_base(lock, plan)
    project = public_metadata_factory(copy.deepcopy(lock), copy.deepcopy(plan))
    if any(key in project and project[key] != value for key, value in common.items()):
        raise PermissionError("Profile metadata changed the common measurement policy")
    plan["public_reproduction"] = {**project, **common}
    output.mkdir(parents=True, exist_ok=False)
    path = output / "bootstrap-protocol.json"
    write(path, plan)
    return path
