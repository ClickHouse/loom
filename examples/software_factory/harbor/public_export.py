"""Export public measurements while keeping native sessions and credentials private."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from examples.software_factory.factory.model import digest

from .runtime import read, verify, write

KIND = "factory-public-results"
COUNTERS = (
    "loom_tools_advertised",
    "tool_total_requests",
    "agent_test_requests",
    "agent_test_runs",
    "loom_tool_calls",
    "memory_rows_written",
    "model_selected_loom_requests",
    "loom_logical_requests",
    "loom_physical_requests",
    "loom_transport_dispatches",
)
TOKEN_FIELDS = (
    "model_uncached_input_tokens",
    "model_input_tokens",
    "model_output_tokens",
    "model_cache_read_tokens",
    "model_cache_write_tokens",
    "model_total_tokens",
    "loom_provider_input_tokens",
    "loom_provider_output_tokens",
    "loom_provider_total_tokens",
    "total_paid_llm_tokens",
    "loom_embedding_tokens",
)
NUMERIC = (
    "generation_cost_usd",
    "loom_provider_cost_usd",
    "total_provider_cost_usd",
    "agent_time_s",
    "end_to_end_s",
    "harbor_wall_s",
    "full_suite_regressions",
    *COUNTERS,
    *TOKEN_FIELDS,
)
PUBLIC_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "huggingface.co",
    "pypi.org",
    "files.pythonhosted.org",
    "nodejs.org",
    "registry.npmjs.org",
    "platform.claude.com",
    "docs.anthropic.com",
}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def count(value):
    return value if type(value) is int and value >= 0 else None


def label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}", value):
        raise ValueError("Public identifiers must be bounded labels")
    return value


def relative(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError("Public file labels must be canonical relative paths")
    return value


def hexdigest(value, length=64):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{" + str(length) + "}", value):
        raise ValueError("A full public source digest is required")
    return value


def url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PUBLIC_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Public source URLs must use an approved public host without credentials")
    return value


def public_metadata(value):
    """Every copied metadata field is an explicit public-profile opt-in."""
    suite, model = value["suite"], value["model"]
    result = {
        "schema_version": 1,
        "suite": {
            "id": label(suite["id"]),
            "repository_url": url(suite["repository_url"]),
            "languages": [label(item) for item in suite["languages"]],
            "source_roots": [relative(item) for item in suite["source_roots"]],
        },
        "model": {key: label(model[key]) for key in ("id", "provider", "api_version")},
        "tasks": [
            {
                "task_id": label(row["task_id"]),
                "base_commit": hexdigest(row["base_commit"], 40),
                "record_sha256": hexdigest(row["record_sha256"]),
                **{
                    key: label(row[key])
                    for key in ("scenario_family", "dependency_task_id")
                    if key in row
                },
            }
            for row in value["tasks"]
        ],
    }

    def public_text(text):
        if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9 ,;:+().'\-]{1,512}", text):
            raise ValueError("Study disclosures must be bounded public prose")
        return text

    if value.get("study_role"):
        result["study_role"] = public_text(value["study_role"])
    if value.get("memory_treatment"):
        result["memory_treatment"] = {
            key: public_text(value["memory_treatment"][key])
            for key in ("availability", "delivery", "measures")
        }
    if value.get("implementation"):
        implementation = value["implementation"]
        result["implementation"] = {
            "repository_url": url(implementation["repository_url"]),
            **{
                key: hexdigest(implementation[key], 40)
                for key in ("controller_commit", "server_commit")
            },
            **{
                key: implementation[key]
                for key in ("controller_dirty", "server_dirty")
                if type(implementation[key]) is bool
            },
            **{
                key: {
                    relative(path): hexdigest(hashed)
                    for path, hashed in implementation[key].items()
                }
                for key in ("controller_files", "server_files")
            },
        }
    dataset = value.get("dataset", {})
    result["dataset"] = {
        key: dataset[key]
        for key in ("repository", "revision", "file", "sha256", "bytes")
        if key in dataset
    }
    if dataset:
        result["dataset"]["repository"] = relative(dataset["repository"])
        result["dataset"]["revision"] = hexdigest(dataset["revision"], 40)
        result["dataset"]["file"] = relative(dataset["file"])
        result["dataset"]["sha256"] = hexdigest(dataset["sha256"])
        if "bytes" in dataset:
            result["dataset"]["bytes"] = number(dataset["bytes"])
        if "selector_policy" in dataset:
            result["dataset"]["selector_policy"] = label(dataset["selector_policy"])
        if "supplemental_records" in dataset:
            result["dataset"]["supplemental_records"] = [
                {
                    "file": relative(row["file"]),
                    "sha256": hexdigest(row["sha256"]),
                    "issue_url": url(row["issue_url"]),
                    "reference_commit": hexdigest(row["reference_commit"], 40),
                }
                for row in dataset["supplemental_records"]
            ]
    deps = value.get("dependencies", {})

    def image(item):
        if not re.fullmatch(r"(?:[A-Za-z0-9_./:-]+@)?sha256:[a-f0-9]{64}", item):
            raise ValueError("An immutable public dependency image is required")
        return item

    def packages(items):
        return [{"name": label(row["name"]), "version": label(row["version"])} for row in items]

    result["dependencies"] = {
        "base_images": [image(item) for item in deps.get("base_images", [])],
        "tasks": [
            {
                "task_id": label(row["task_id"]),
                "image": image(row["image"]),
                "packages": packages(row["packages"]),
                "build_packages": packages(row["build_packages"]),
            }
            for row in deps.get("tasks", [])
        ],
        "package_artifacts": [
            {**packages([row])[0], "sha256": [hexdigest(item) for item in row["sha256"]]}
            for row in deps.get("package_artifacts", [])
        ],
    }
    tools = value.get("indexers", {})
    result["indexers"] = {"tools": packages(tools.get("tools", []))}
    if "environment_packages" in tools:
        result["indexers"]["environment_packages"] = packages(tools["environment_packages"])
    if tools.get("recipe_sha256"):
        result["indexers"]["recipe_sha256"] = hexdigest(tools["recipe_sha256"])
    pricing = value.get("pricing", {})
    result["pricing"] = {
        key: number(pricing[key])
        for key in (
            "input_usd_per_million",
            "output_usd_per_million",
            "prompts_over_100000_input",
            "prompts_over_100000_output",
        )
        if key in pricing
    }
    if pricing.get("source"):
        result["pricing"]["source"] = url(pricing["source"])
    result["attributions"] = [
        {"name": label(row["name"]), "url": url(row["url"]), "sha256": hexdigest(row["sha256"])}
        for row in value.get("attributions", [])
    ]
    return result


def export_public(protocol, output: Path, *, public_config=None, require_complete=False):
    protocol = protocol if isinstance(protocol, dict) else read(protocol)
    verify(protocol)
    root, output = Path(protocol["output_root"]).resolve(), Path(output)
    if (
        output.is_symlink()
        or output.resolve() == root
        or output.resolve().is_relative_to(root / "jobs")
    ):
        raise PermissionError("Public output must be separate from native results")
    entries_path = root / "live-attempts.json"
    entries = read(entries_path) if entries_path.exists() else []
    from .billing import audit_provider_costs
    from .profiles import load_profile
    from .study import enrich_evaluations, result_record

    derived = []
    for entry in entries:
        order = entry["order"]
        slot = protocol["schedule"][order]
        trial = Path(entry["result"]["harbor_trial"])
        if trial.is_symlink() or not trial.resolve().is_relative_to(root / "jobs/live"):
            raise PermissionError("Public export requires the original native trial")
        path = trial / "result.json"
        if path.is_symlink():
            raise PermissionError("Native result cannot be a symlink")
        raw = read(path)
        if Path(raw["config"]["task"]["path"]) != root / "tasks" / f"slot-{order:02d}":
            raise PermissionError("Native task differs from the frozen attempt")
        result = result_record(
            slot,
            raw,
            trial,
            task_kind=load_profile(protocol).TASK_KIND,
            model_id=protocol["model_id"],
        )
        result["protocol_sha256"] = digest(protocol)
        derived.append({"order": order, "result": result})
    if (root / "provider-ledger.json").exists():
        audit_provider_costs(protocol, derived)
    enrich_evaluations(protocol, derived, "live")
    entries = derived
    complete = len(entries) == len(protocol["schedule"])
    if require_complete and not complete:
        raise PermissionError("Native cohort is incomplete")
    output.mkdir(parents=True, exist_ok=True)
    marker = output / "ownership.json"
    identity = {"kind": KIND, "protocol_sha256": digest(protocol)}
    if marker.exists() and read(marker) != identity:
        raise PermissionError("Public bundle belongs to another study")
    guard = output / ".export.lock"
    with guard.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        write(marker, identity)
        public = []
        hashes = {}
        for entry in entries:
            result = entry["result"]
            slot = protocol["schedule"][entry["order"]]
            row = {
                key: slot[key]
                for key in ("order", "task_id", "arm", "repeat", "memory_enabled", "index_tier")
            }
            row.update({key: number(result.get(key)) for key in NUMERIC})
            row.update(
                resolved=result["resolved"],
                judge_fixed=result.get("judge_fixed"),
                harbor_reward_valid=result.get("harbor_reward_valid"),
                generation_cost_source=result.get("generation_cost_source"),
                loom_connection_at_start=result.get("loom_connection_at_start"),
                loom_plugin_loaded=result.get("loom_plugin_loaded"),
                loom_code_skill_loaded=result.get("loom_code_skill_loaded"),
                loom_memory_skill_loaded=result.get("loom_memory_skill_loaded"),
            )
            trial = Path(result["harbor_trial"])
            for name in (
                "result.json",
                "agent/trajectory.json",
                "agent/claude-code.txt",
                "verifier/grade.json",
                "verifier/full-suite.json",
            ):
                path = trial / name
                if path.exists():
                    hashes[str(entry["order"]) + "/" + name] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
            public.append(row)
            instruction = root / "tasks" / f"slot-{entry['order']:02d}" / "instruction.md"
            hashes[str(entry["order"]) + "/task/instruction.md"] = hashlib.sha256(
                instruction.read_bytes()
            ).hexdigest()
        payload = {
            "kind": KIND,
            "complete": complete,
            "metadata": public_metadata(public_config or protocol["public_reproduction"]),
            "solver": protocol["solver"],
            "attempts": public,
            "native_artifact_sha256": hashes,
            "cost_scope": (
                "Native Claude Code reported cost plus authenticated Loom provider ledger; "
                "setup and judging separate."
            ),
        }
        write(output / "results.json", payload)
        files = {"results.json": hashlib.sha256((output / "results.json").read_bytes()).hexdigest()}
        write(output / "manifest.json", {**identity, "files": files})
    return {"complete": complete, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            export_public(args.protocol, args.output, require_complete=args.require_complete)
        )
    )


if __name__ == "__main__":
    main()
