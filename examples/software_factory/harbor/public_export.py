"""Export allowlisted public measurements from original native Harbor results."""

from __future__ import annotations

import argparse
import copy
import csv
import fcntl
import hashlib
import json
import math
import re
import shutil
import tempfile
from dataclasses import fields
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from harbor.models.trial.result import TrialResult

from examples.software_factory.factory.model import Budget, digest

from .charts import ARMS, generate_charts
from .metrics import token_totals

KIND = "factory-public-results"
ALL_ARMS = (*ARMS, "baseline-a", "baseline-b")
COUNTERS = (
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
            }
            for row in value["tasks"]
        ],
    }
    if value.get("implementation"):
        result["implementation"] = {
            "repository_url": url(value["implementation"]["repository_url"]),
            "commit": hexdigest(value["implementation"]["commit"], 40),
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


class Inputs:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.paths = {}
        self.source_files = {}

    def raw(self, path, name):
        path = Path(path)
        parents = (
            [parent for parent in path.parents if parent.is_relative_to(self.root)]
            if path.is_relative_to(self.root)
            else []
        )
        if path.is_symlink() or any(parent.is_symlink() for parent in parents):
            raise PermissionError("Public export refuses symlinked native inputs")
        raw = path.read_bytes()
        hashed = hashlib.sha256(raw).hexdigest()
        self.paths[path] = hashed
        self.source_files[relative(name)] = hashed
        return raw

    def read(self, path, name):
        return json.loads(self.raw(path, name))

    def unchanged(self):
        if any(
            hashlib.sha256(path.read_bytes()).hexdigest() != expected
            for path, expected in self.paths.items()
        ):
            raise PermissionError("Native evidence changed during public export")


def schedule(protocol, metadata):
    rows = protocol["schedule"]
    if digest(rows) != protocol["schedule_sha256"] or len(rows) != 33:
        raise ValueError("Public comparison requires the complete frozen 33-slot schedule")
    tasks = {row["task_id"] for row in metadata["tasks"]}
    expected = {(task, repeat, arm) for task in tasks for repeat in range(3) for arm in ARMS}
    expected |= {(task, "aa", arm) for task in tasks for arm in ("baseline-a", "baseline-b")}
    if len(tasks) != 3 or {(r["task_id"], r["repeat"], r["arm"]) for r in rows} != expected:
        raise ValueError("Schedule must retain all primary repeats and separate A/A controls")
    result = []
    for order, row in enumerate(rows):
        tier = {"loom-treesitter": "treesitter", "loom-scip": "scip"}.get(row["arm"])
        if (
            row["order"] != order
            or row["mode"] != ("natural" if tier else "baseline")
            or row.get("index_tier") != tier
        ):
            raise ValueError("Scheduled arm, mode or tier changed")
        result.append(
            {
                "order": order,
                "task_id": row["task_id"],
                "repeat": row["repeat"],
                "arm": row["arm"],
                "index_tier": tier,
            }
        )
    return result


def native_trials(root, kind, slots, inputs):
    found = {}
    for ordinal, folder in enumerate(sorted((root / "jobs" / kind).glob("*"))):
        if not folder.is_dir():
            continue
        if folder.is_symlink():
            raise PermissionError("Public export refuses symlinked native trials")
        path = folder / "result.json"
        if not path.exists():
            continue
        source_label = f"native/{kind}/record-{ordinal:02d}/result.json"
        raw = inputs.read(path, source_label)
        native = TrialResult.model_validate(raw)
        match = re.fullmatch(r"slot-(\d+)", Path(raw["config"]["task"]["path"]).name)
        if not match:
            raise ValueError("Native trial does not identify a scheduled slot")
        order = int(match[1])
        if order not in range(len(slots)) or order in found or native.trial_name != folder.name:
            raise ValueError("Duplicate or unscheduled native result")
        if (
            Path(raw["config"]["task"]["path"]).resolve()
            != (root / "tasks" / f"slot-{order:02d}").resolve()
        ):
            raise ValueError("Native result belongs to another study")
        inputs.source_files[f"native/{kind}/slot-{order:02d}/result.json"] = (
            inputs.source_files.pop(source_label)
        )
        if not native.finished_at:
            continue
        if "reward_valid" in raw and raw["reward_valid"] is not native.reward_valid:
            raise ValueError("Native reward validity differs from Harbor's exception policy")
        # Opaque slot labels replace private original trial directory names.
        found[order] = (folder, raw, native)
    return found


def full_suite(value):
    if (
        not isinstance(value, dict)
        or value.get("valid") is not True
        or not isinstance(value.get("tests"), dict)
    ):
        return None
    statuses = value["tests"]
    if (
        not statuses
        or value.get("status") != "completed"
        or any(status not in {"PASSED", "FAILED", "SKIPPED"} for status in statuses.values())
        or value.get("exit_code") != int(any(s == "FAILED" for s in statuses.values()))
    ):
        return None
    return statuses


def aggregate(slots, attempts):
    rows = [attempts[row["order"]] for row in slots]
    observed = [row for row in rows if row["state"] != "pending"]

    def total(key):
        values = [row.get(key) for row in observed]
        return sum(values) if values and None not in values else None

    return {
        "metric_scope": "observed_attempts_only",
        "scheduled": len(rows),
        "observed": len(observed),
        "pending": len(rows) - len(observed),
        "resolved": sum(row["resolved"] is True for row in observed),
        "invalid_verification": sum(row["harbor_reward_valid"] is False for row in observed),
        **{key: total(key) for key in (*NUMERIC, "judge_fixed")},
    }


def source_labels(protocol):
    root = Path(protocol.get("working_directory", Path(__file__).resolve().parents[3])).resolve()
    result = {}
    for name, hashed in {
        **protocol.get("files", {}),
        **protocol.get("server_source_identity", {}).get("files", {}),
    }.items():
        path = Path(name)
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = None
            for part in ("server", "schemas"):
                if part in path.parts:
                    candidate = Path(*path.parts[path.parts.index(part) :])
                    if (root / candidate).is_file():
                        rel = candidate
                        break
        if (
            rel
            and (root / rel).is_file()
            and rel.parts[0] in {"server", "schemas", "sdk", "examples"}
            and rel.suffix in {".py", ".json", ".sql", ".html", ".js", ".in", ".yaml", ".txt"}
        ):
            result[relative(str(rel))] = hexdigest(hashed)
    return result


def index_proofs(protocol, tasks):
    output = []
    for task in tasks:
        indexes = protocol.get("tasks", {}).get(task["task_id"], {}).get("indexes", {})
        for tier in ("treesitter", "scip"):
            scope = indexes.get(tier)
            if not scope:
                continue
            source = scope.get("source_scope", {})
            proof = {
                "task_id": task["task_id"],
                "tier": tier,
                "compiler_rows": {
                    key: count(scope.get("compiler_rows", {}).get(key))
                    for key in (
                        "compiler_definitions",
                        "compiler_references",
                        "python_definitions",
                        "python_references",
                    )
                },
                "file_count": count(source.get("file_count")),
            }
            for key in ("source_sha256", "paths_sha256"):
                proof[key] = hexdigest(source[key]) if source.get(key) else None
            output.append(proof)
    return output


def measurements(protocol, slots, inputs):
    root = Path(protocol["output_root"]).resolve()
    trials = native_trials(root, "live", slots, inputs)
    bases = native_trials(root, "base", slots, inputs)
    base_suites = {}
    for order, (folder, _, _) in bases.items():
        path = folder / "verifier/full-suite.json"
        if path.exists():
            base_suites[slots[order]["task_id"]] = full_suite(
                inputs.read(path, f"native/base/slot-{order:02d}/full-suite.json")
            )
    verdicts, costs = {}, {}
    for name, target in (("judgements/verdicts.json", verdicts), ("provider-costs.json", costs)):
        path = root / name
        if path.exists():
            saved = inputs.read(path, "evidence/" + name)
            if saved.get("protocol_sha256") != digest(protocol):
                raise ValueError("Supplementary measurements belong to another protocol")
            target.update(saved.get("attempts", {}))
    attempts, chart_entries = [], []
    for slot in slots:
        order = slot["order"]
        item = {
            **slot,
            "state": "pending",
            "resolved": None,
            "harbor_reward_valid": None,
            **dict.fromkeys(
                (
                    *NUMERIC,
                    "judge_fixed",
                    "full_suite_tests",
                    "full_suite_failed",
                    "full_suite_skipped",
                )
            ),
        }
        if order not in trials:
            attempts.append(item)
            continue
        folder, raw, native = trials[order]
        context = raw.get("agent_result") or {}
        factory = (context.get("metadata") or {}).get("factory") or {}
        if factory.get("task_id", slot["task_id"]) != slot["task_id"]:
            raise ValueError("Native factory task differs from its scheduled identity")
        if factory.get("protocol_sha256", digest(protocol)) != digest(protocol):
            raise ValueError("Native factory metrics belong to another frozen protocol")
        valid = native.reward_valid
        reward = (raw.get("verifier_result") or {}).get("rewards", {}).get("reward")
        if valid and (type(reward) not in (int, float) or reward not in (0, 1)):
            raise ValueError("This comparison requires the native binary hidden reward")
        item.update(
            state="observed" if valid else "invalid_verifier",
            resolved=valid and reward == 1,
            harbor_reward_valid=valid,
            **{key: count(factory.get(key)) for key in COUNTERS},
            **token_totals(factory),
        )
        item["agent_time_s"] = number(factory.get("agent_time_s"))
        item["generation_cost_usd"] = number(
            factory.get("generation_cost_usd")
            if "generation_cost_usd" in factory
            else context.get("cost_usd")
        )
        native_cost = number(context.get("cost_usd"))
        if (
            item["generation_cost_usd"] is not None
            and native_cost is not None
            and item["generation_cost_usd"] != native_cost
        ):
            item["generation_cost_usd"] = None
        if native.started_at:
            item["harbor_wall_s"] = number((native.finished_at - native.started_at).total_seconds())
        path = folder / "factory-measurement.json"
        if path.exists():
            item["end_to_end_s"] = number(
                inputs.read(path, f"native/live/slot-{order:02d}/measurement.json").get(
                    "end_to_end_s"
                )
            )
        path = folder / "verifier/grade.json"
        grade = (
            inputs.read(path, f"native/live/slot-{order:02d}/grade.json") if path.exists() else {}
        )
        path = folder / "verifier/full-suite.json"
        suite = (
            full_suite(inputs.read(path, f"native/live/slot-{order:02d}/full-suite.json"))
            if path.exists()
            else None
        )
        if suite is not None:
            item.update(
                full_suite_tests=len(suite),
                full_suite_failed=sum(s == "FAILED" for s in suite.values()),
                full_suite_skipped=sum(s == "SKIPPED" for s in suite.values()),
            )
            base = base_suites.get(slot["task_id"])
            if base is not None:
                item["full_suite_regressions"] = sum(
                    status == "PASSED" and suite.get(name) != "PASSED"
                    for name, status in base.items()
                )
        verdict = verdicts.get(str(order), {})
        if (
            grade.get("state")
            and verdict.get("candidate_state") == grade["state"]
            and type(verdict.get("judge_fixed")) is bool
        ):
            item["judge_fixed"] = verdict["judge_fixed"]
        bill = costs.get(str(order), {})
        trace = folder / "agent/loom-transport.jsonl"
        events = (
            [
                json.loads(line)
                for line in inputs.raw(
                    trace, f"native/live/slot-{order:02d}/loom-transport.jsonl"
                ).splitlines()
            ]
            if trace.exists()
            else []
        )
        if (
            all(
                factory.get(key) == 0 and type(factory.get(key)) is int
                for key in (
                    "loom_logical_requests",
                    "loom_physical_requests",
                    "model_selected_loom_requests",
                    "loom_transport_dispatches",
                )
            )
            and not events
            and (
                bill.get("proof_status") != "known"
                or (
                    bill.get("physical_requests_dispatched")
                    == bill.get("physical_requests_matched")
                    == 0
                    and bill.get("loom_provider_cost_usd") == 0
                )
            )
        ):
            item["loom_provider_cost_usd"] = 0
        elif (
            bill.get("proof_status") == "known"
            and count(factory.get("loom_physical_requests")) is not None
            and factory["loom_physical_requests"] > 0
            and factory.get("loom_transport_dispatches") == factory["loom_physical_requests"]
            and bill.get("physical_requests_dispatched") == factory.get("loom_physical_requests")
            and bill.get("physical_requests_matched") == factory.get("loom_physical_requests")
        ):
            item["loom_provider_cost_usd"] = number(bill.get("loom_provider_cost_usd"))
        values = [item["generation_cost_usd"], item["loom_provider_cost_usd"]]
        item["total_provider_cost_usd"] = sum(values) if None not in values else None
        attempts.append(item)
        chart_entries.append({"order": order, "result": item})
    return attempts, chart_entries


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def tree_hashes(folder):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def evaluator_overhead(protocol, inputs):
    folder = Path(protocol["output_root"]) / "judgements"
    records = []
    for path in sorted(folder.glob("pair-[0-9][0-9].json")):
        record = inputs.read(path, f"evidence/judging/request-{len(records):02d}.json")
        if record.get("protocol_sha256") != digest(protocol):
            raise ValueError("Judge receipt belongs to another frozen protocol")
        records.append(record)

    def total(key):
        values = [number(row.get(key)) for row in records]
        return sum(values) if values and None not in values else None

    return {
        "judge": {
            "enabled": protocol.get("judge", {}).get("enabled") is True,
            "completed_receipts": len(records),
            "generation_cost_usd": total("generation_cost_usd"),
            "duration_s": total("duration_s"),
            "excluded_from_attempt_provider_cost_and_time": True,
        }
    }


def _export(protocol, output, metadata, require_complete):
    if output.exists():
        owner = output / ".public-export.json"
        if (
            not owner.is_file()
            or owner.is_symlink()
            or json.loads(owner.read_text()) != {"kind": KIND, "protocol_sha256": digest(protocol)}
        ):
            raise PermissionError("Refuse to overwrite an unowned public results directory")
        manifest = json.loads((output / "manifest.json").read_text())
        if any(p.is_symlink() for p in output.rglob("*")) or tree_hashes(output) != {
            **manifest["files"],
            "manifest.json": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest(),
        }:
            raise PermissionError("Owned public output was modified outside the exporter")
    root = Path(protocol["output_root"]).resolve()
    if output == root or root.is_relative_to(output) or output.is_relative_to(root / "jobs"):
        raise PermissionError("Public output must be separate from native Harbor results")
    public = public_metadata(metadata)
    if public["model"]["id"] != protocol["model_id"]:
        raise ValueError("Public model differs from the measured protocol")
    slots = schedule(protocol, public)
    inputs = Inputs(root)
    historical = inputs.read(protocol["historical_manifest"], "inputs/public-tasks.json")
    bases = {r["task_id"]: r["base_commit"] for r in historical["tasks"]}
    if any(bases.get(row["task_id"]) != row["base_commit"] for row in public["tasks"]):
        raise ValueError("Public task bases differ from the actual task inputs")
    attempts, chart_entries = measurements(protocol, slots, inputs)
    job_path = root / "jobs/live/result.json"
    job = inputs.read(job_path, "native/live/job-result.json") if job_path.exists() else {}
    complete = all(row["state"] != "pending" for row in attempts) and bool(job.get("finished_at"))
    if require_complete and not complete:
        raise ValueError("Incomplete cohort cannot be exported as a final comparison")
    public.update(
        schedule=slots,
        schedule_sha256=digest(slots),
        private_schedule_sha256=protocol["schedule_sha256"],
        source_files=source_labels(protocol),
        indexes=index_proofs(protocol, public["tasks"]),
        execution={
            "controller": "harbor",
            "harbor_version": label(protocol["harbor_version"])
            if protocol.get("harbor_version")
            else None,
            "attempt_budget": {
                field.name: number(protocol["attempt_budget"].get(field.name))
                for field in fields(Budget)
                if field.name in protocol.get("attempt_budget", {})
            },
            "visible_test_call_limit": number(protocol.get("visible_test_call_limit")),
            "max_physical_loom_requests": number(protocol.get("max_physical_loom_requests")),
        },
        policy={
            "all_scheduled_attempts_included": True,
            "aa_excluded_from_primary": True,
            "objective_quality": "native independent hidden reward",
            "cost": "generation list-price estimate plus correlated Loom ledger cost; "
            "unknown remains null; invoices and infrastructure excluded",
            "judge": "separate blinded subjective assessment; no candidate ranking",
            "stochastic_outputs": True,
        },
    )
    stage = Path(tempfile.mkdtemp(prefix=".public-export-", dir=output.parent))
    try:
        write(stage / ".public-export.json", {"kind": KIND, "protocol_sha256": digest(protocol)})
        write(stage / "protocol.json", public)
        write(stage / "attempts.json", attempts)
        summary = {
            "schema_version": 1,
            "complete": complete,
            "observed": len(chart_entries),
            "scheduled": len(slots),
            "primary": {},
            "aa_controls": {},
            "all_attempts": aggregate(slots, attempts),
        }
        for arm in ALL_ARMS:
            target = summary["primary"] if arm in ARMS else summary["aa_controls"]
            target[arm] = aggregate([row for row in slots if row["arm"] == arm], attempts)
        write(stage / "summary.json", summary)
        scenarios = [
            {
                "task_id": task,
                "arms": {
                    arm: aggregate(
                        [row for row in slots if row["task_id"] == task and row["arm"] == arm],
                        attempts,
                    )
                    for arm in ALL_ARMS
                },
            }
            for task in sorted(bases)
        ]
        write(stage / "scenarios.json", scenarios)
        write(stage / "evaluators.json", evaluator_overhead(protocol, inputs))
        with (stage / "attempts.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(attempts[0]))
            writer.writeheader()
            writer.writerows(attempts)
        chart_root = stage / "charts"
        write(chart_root / "tasks.json", {"tasks": public["tasks"]})
        generate_charts(
            {
                "output_root": str(chart_root),
                "model_id": public["model"]["id"],
                "historical_manifest": str(chart_root / "tasks.json"),
                "schedule": slots,
                "reporting": {"publish_charts": False},
            },
            chart_entries,
            {},
        )
        data_path = chart_root / "reports/charts.json"
        original = data_path.read_text().strip()
        data = json.loads(original)
        data.update(name="Public factory comparison", run_id=digest(public)[:16])
        encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        html_path = chart_root / "reports/charts.html"
        html = html_path.read_text()
        if original not in html:
            raise ValueError("Standalone chart data differs from its embedded document")
        html_path.write_text(html.replace(original, encoded, 1))
        data_path.write_text(encoded + "\n")
        write(
            stage / "provenance.json",
            {
                "source_protocol_sha256": digest(protocol),
                "source_files": inputs.source_files,
                "raw_native_files_unchanged": True,
            },
        )
        (stage / "README.md").write_text(
            "# Public factory results\n\n"
            + ("Complete" if complete else "Pending")
            + f" cohort: {len(chart_entries)} of {len(slots)} attempts observed.\n\n"
            "Every scheduled failure and A/A control is retained. Unknown measurements are null. "
            "The charts compare primary arms; A/A controls have separate tables. "
            "Judge opinions are separate from independent hidden-test success. "
            "No automatic performance or merge claim is made.\n\n"
            "Repeat the published code and inputs with your own credentials. "
            "Model answers, latency and cost can vary between runs.\n"
        )
        inputs.unchanged()
        manifest = {
            "schema_version": 1,
            "kind": KIND,
            "complete": complete,
            "files": tree_hashes(stage),
        }
        write(stage / "manifest.json", manifest)
        backup = None
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix=".public-backup-", dir=output.parent))
            backup.rmdir()
            output.rename(backup)
        try:
            stage.rename(output)
        except BaseException:
            if backup is not None:
                backup.rename(output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def export_public(protocol, output: Path, *, public_config=None, require_complete=False):
    """Offline export; refreshing an owned bundle never changes native results."""
    protocol = copy.deepcopy(
        protocol if isinstance(protocol, dict) else json.loads(Path(protocol).read_text())
    )
    metadata = public_config if public_config is not None else protocol["public_reproduction"]
    if not isinstance(metadata, dict):
        metadata = json.loads(Path(metadata).read_text())
    output = Path(output)
    if output.is_symlink():
        raise PermissionError("Public output cannot be a symlink")
    output = output.resolve()
    root = Path(protocol["output_root"]).resolve()
    if output == root or root.is_relative_to(output) or output.is_relative_to(root / "jobs"):
        raise PermissionError("Public output must be separate from native Harbor results")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / ("." + output.name + ".public-export.lock")
    if lock.is_symlink():
        raise PermissionError("Public export lock cannot be a symlink")
    with lock.open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        return _export(protocol, output, metadata, require_complete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-config", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    result = export_public(
        args.protocol,
        args.output,
        public_config=args.public_config,
        require_complete=args.require_complete,
    )
    print(json.dumps({"complete": result["complete"], "files": len(result["files"])}))


if __name__ == "__main__":
    main()
