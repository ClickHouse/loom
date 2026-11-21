"""Published project data added to the shared reproduction method."""

import hashlib
import json
from pathlib import Path

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.planning import public_reproduction_base


def public_reproduction(lock, protocol):
    def packages(requirements):
        return [
            dict(zip(("name", "version"), item.split("==", 1), strict=True))
            for item in requirements
        ]

    pins = {row["task_id"]: row for row in lock["tasks"]}
    public = json.loads(Path(protocol["historical_manifest"]).read_text())["tasks"]
    if {row["task_id"] for row in public} != set(protocol["tasks"]):
        raise PermissionError("Public scenario metadata does not cover the frozen tasks")
    tasks, dependencies = [], []
    for row in public:
        task_id = row["task_id"]
        config = protocol["tasks"][task_id]
        if task_id in pins:
            dependency_id, family = task_id, "historical-issue"
            record_sha = pins[task_id]["record_sha256"]
        else:
            fixture = json.loads(Path(config["public_fixture_file"]).read_text())
            dependency_id, family = fixture["dependency_task_id"], fixture["family"]
            if fixture["case_id"] != task_id:
                raise PermissionError("Authored scenario and dependency fixture identities differ")
            record_sha = digest(row)
        if "scenario_family" in row and row["scenario_family"] != family:
            raise PermissionError("Public scenario family differs from its source fixture")
        pin = pins[dependency_id]
        tasks.append(
            dict(
                task_id=task_id,
                base_commit=row["base_commit"],
                record_sha256=record_sha,
                scenario_family=family,
                dependency_task_id=dependency_id,
            )
        )
        dependencies.append(
            dict(
                task_id=task_id,
                dependency_task_id=dependency_id,
                image=config["image"],
                packages=packages(pin["requirements"]),
                build_packages=packages(pin["build_requirements"]),
            )
        )
    return {
        **public_reproduction_base(lock, protocol),
        "study_role": "development regression; prior task outcomes were inspected",
        "memory_treatment": {
            "availability": "Historical knowledge is available only to memory-enabled arms",
            "delivery": "Optional native memory tools and code-linked lessons; no prompt injection",
            "measures": "Historical knowledge plus its native retrieval, not linking in isolation",
        },
        "dataset": {
            key: lock["dataset"][key]
            for key in ("repository", "revision", "file", "sha256", "bytes")
        },
        "tasks": tasks,
        "dependencies": {
            "base_images": [lock["python_image"]],
            "package_artifacts": [
                {**packages([name])[0], "sha256": hashes}
                for name, hashes in lock["package_sha256"].items()
            ],
            "tasks": dependencies,
        },
        "indexers": {
            "environment_packages": packages(lock["compiler_tools"]["dependency_environment"]),
            "recipe_sha256": hashlib.sha256(
                Path(__file__).with_name("compiler.py").read_bytes()
            ).hexdigest(),
            "tools": [
                {"name": name, "version": lock["compiler_tools"][key]}
                for name, key in (
                    ("scip-python", "scip_python"),
                    ("node", "node"),
                    ("python-language", "python_language_version"),
                )
            ],
        },
        "attributions": [
            {key: row[key] for key in ("name", "url", "sha256")} for row in lock["attributions"]
        ],
    }
