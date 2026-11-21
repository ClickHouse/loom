"""Public reproduction inputs from the repository profile's published lock."""

import hashlib
from pathlib import Path


def public_reproduction(lock, protocol):
    def packages(requirements):
        return [
            dict(zip(("name", "version"), item.split("==", 1), strict=True))
            for item in requirements
        ]

    return {
        "schema_version": 1,
        "suite": {
            "id": lock["id"],
            "repository_url": lock["repository_url"],
            "languages": lock["languages"],
            "source_roots": lock["source_roots"],
        },
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
        "dataset": {
            key: lock["dataset"][key]
            for key in ("repository", "revision", "file", "sha256", "bytes")
        },
        "tasks": [
            {
                "task_id": row["task_id"],
                "base_commit": row["base_commit"],
                "record_sha256": row["record_sha256"],
            }
            for row in lock["tasks"]
        ],
        "dependencies": {
            "base_images": [lock["python_image"]],
            "package_artifacts": [
                {**packages([name])[0], "sha256": hashes}
                for name, hashes in lock["package_sha256"].items()
            ],
            "tasks": [
                {
                    "task_id": row["task_id"],
                    "image": protocol["tasks"][row["task_id"]]["image"],
                    "packages": packages(row["requirements"]),
                    "build_packages": packages(row["build_requirements"]),
                }
                for row in lock["tasks"]
            ],
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
        "attributions": [
            {key: row[key] for key in ("name", "url", "sha256")} for row in lock["attributions"]
        ],
    }
