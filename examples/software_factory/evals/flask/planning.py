"""Join public source fixtures and certified native setup into a frozen study."""

from __future__ import annotations

import json
from pathlib import Path

from examples.software_factory.evals.flask import bootstrap
from examples.software_factory.evals.flask.historical import FrozenSource
from examples.software_factory.evals.flask.public_metadata import public_reproduction
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.planning import freeze_plan
from examples.software_factory.harbor.runtime import read, write


def inputs(assets: Path, materialization: dict, output: Path) -> dict:
    lock = read(bootstrap.LOCK)
    asset_receipt, images = read(assets / "assets.json"), read(assets / "images.json")
    bootstrap.check_files(asset_receipt["files"])
    bootstrap.check_files(images["artifact_files"])
    bootstrap.check_files(materialization["files"])
    public = read(materialization["historical_manifest"])["tasks"]
    metadata = {row["task_id"]: row for row in public}
    tasks, histories = {}, {}
    output.mkdir(parents=True, exist_ok=False)
    records = {row["instance_id"]: row for row in read(assets / "trusted/tasks.json")}
    for pin in lock["tasks"]:
        task_id = pin["task_id"]
        record = records[task_id]
        tasks[task_id] = {
            "source_git": asset_receipt["source_git"],
            "symbol": pin["symbol"],
            "image": images["images"][task_id]["image"],
            "evaluation_file": str(assets / "trusted" / (task_id + ".json")),
            "visible_selectors": pin["visible_selectors"],
            "hidden_selectors": list(
                dict.fromkeys(
                    json.loads(record["FAIL_TO_PASS"]) + json.loads(record["PASS_TO_PASS"])
                )
            ),
            "scenario_family": "historical-issue",
        }
    tasks.update({task_id: dict(config) for task_id, config in materialization["tasks"].items()})
    for task_id, config in tasks.items():
        row = metadata[task_id]
        config.update(
            repository=row["repository"],
            base_commit=row["base_commit"],
            source_roots=lock["source_roots"],
        )
        if task_id in materialization["fixtures"]:
            fixture = Path(materialization["fixtures"][task_id]["fixture_file"])
            config["dependency_task_id"] = read(fixture)["dependency_task_id"]
        else:
            config["dependency_task_id"] = task_id
            source = FrozenSource(Path(config["source_git"]), row["base_commit"])
            files = source.files("src/flask")
            fixture = output / (task_id + ".json")
            write(
                fixture,
                {
                    "schema_version": 1,
                    "kind": "cold",
                    "case_id": task_id,
                    "source_git": config["source_git"],
                    "revisions": {"B": {"commit": row["base_commit"]}},
                    "source_sha256": {"B": digest(files)},
                    "public_documents": {},
                    "history_events": [],
                },
            )
        histories[task_id] = {
            "fixture_file": str(fixture),
            "fixture_sha256": bootstrap.sha(fixture),
        }
    frozen = {
        **asset_receipt["files"],
        **images["artifact_files"],
        **materialization["files"],
        **{row["fixture_file"]: row["fixture_sha256"] for row in histories.values()},
    }
    result = {
        "tasks": tasks,
        "memory_histories": histories,
        "historical_manifest": materialization["historical_manifest"],
        "reference_controls_file": materialization["reference_controls_file"],
        "public_inputs": frozen,
        "schedule": schedule(
            sorted(tasks),
            seed=lock["schedule_seed"],
            task_families={task: row["scenario_family"] for task, row in tasks.items()},
        ),
    }
    write(output / "inputs.json", result)
    return result


def freeze(prepared, setup, output, **kwargs):
    return freeze_plan(
        prepared,
        setup,
        output,
        lock=read(bootstrap.LOCK),
        public_metadata_factory=public_reproduction,
        **kwargs,
    )
