"""Prepare, run and report a frozen repository cohort with Harbor."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from statistics import fmean
from urllib.parse import urlsplit

from harbor.job import Job
from harbor.models.job.config import JobConfig

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.index import MUTABLE_STATE_KEYS, select_snapshot, snapshot
from examples.software_factory.harbor.paired import ARMS, analyze, analyze_cohorts
from examples.software_factory.harbor.reporting import generate_reports
from examples.software_factory.harbor.runtime import read, task_inputs, verify, write

from .billing import FIELDS as BILLING_FIELDS
from .billing import audit_provider_costs
from .fairness import audit_fairness
from .judging import load_verdicts, run_judging
from .metrics import known_total, paid_token_totals, token_totals
from .profiles import load_profile
from .public_export import export_public
from .scopes import selected_scopes

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified_input_files(plan):
    paths = set()
    for identity in (
        plan["public_inputs"],
        plan["server_source_identity"]["files"],
        plan["controller_source_identity"]["files"],
    ):
        if not identity:
            raise PermissionError("Public input and server source fingerprints are required")
        for name, expected in identity.items():
            path = Path(name)
            if not path.is_absolute() or file_hash(path) != expected:
                raise PermissionError("A public input or server source changed before freezing")
            paths.add(path)
    return paths


def verify_qualifications(protocol):
    output = Path(protocol["output_root"])
    expected = digest(protocol)
    for kind in ("base", "reference"):
        receipt = read(output / (kind + "-qualification.json"))
        results = [entry["result"] for entry in receipt.get("attempts", [])]
        if (
            receipt.get("qualified") is not True
            or receipt.get("protocol_sha256") != expected
            or len(results) != len(protocol["tasks"])
            or {r["task_id"] for r in results} != set(protocol["tasks"])
            or any(
                r.get("protocol_sha256") != expected
                or r.get("harbor_reward_valid") is not True
                or r.get("resolved") is not (kind == "reference")
                or r.get("provider_physical_requests") != 0
                or (r.get("full_suite") or {}).get("valid") is not True
                or (kind == "reference" and r.get("full_suite_regressions") != 0)
                for r in results
            )
        ):
            raise PermissionError("Native Harbor controls do not qualify this exact protocol")


def stage_sandbox_policy(folder: Path, *, verifier: bool = False) -> Path:
    relative = Path("tests/docker-compose.yaml") if verifier else Path("sandbox-compose.yaml")
    target = folder / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "sandbox-compose.yaml", target)
    return target


def build_image(folder: Path, dependency: str) -> str:
    inspected = json.loads(
        subprocess.run(
            ["docker", "image", "inspect", dependency],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    )[0]
    if inspected["Id"] != dependency or not inspected.get("RepoTags"):
        raise PermissionError("Dependency image has no verified local build reference")
    reference = inspected["RepoTags"][0]
    (folder / "Dockerfile").write_text(
        "FROM "
        + reference
        + "\nUSER root\nCOPY factory /factory\n"
        + ("COPY factory/tests /tests\n" if (folder / "factory/tests").exists() else "")
        + "ENV PYTHONDONTWRITEBYTECODE=1\nENV PYTHONPATH=/factory/lib\n"
        + "RUN python -c \"import runpy; runpy.run_path('/factory/grader.py', "
        + "run_name='grader_import_check')\"\nWORKDIR /workspace\n"
    )
    receipt = folder / "image.id"
    with (folder / "build.log").open("wb") as log:
        subprocess.run(
            [
                "docker",
                "build",
                "--network=none",
                "--iidfile",
                str(receipt),
                "--label",
                "loom.factory.harbor=task-runtime",
                str(folder),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=True,
        )
    actual = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    if actual != dependency:
        raise PermissionError("Dependency image changed during the build")
    return receipt.read_text().strip()


def prepare(previous: Path, output: Path) -> Path:
    plan = read(previous)
    if not plan.get("public_reproduction"):
        raise ValueError("A public reproduction manifest is required")
    inputs = verified_input_files(plan)
    endpoint = urlsplit(plan["loom_url"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ValueError("An explicit HTTP(S) Loom endpoint is required")
    if not plan.get("server_container"):
        raise ValueError("The owned server container must be supplied separately from its URL")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    original = json.loads(json.dumps(plan))
    sandbox_policy = stage_sandbox_policy(output)
    plan["sandbox_policy"] = {"path": str(sandbox_policy), "sha256": file_hash(sandbox_policy)}
    plan.update(
        output_root=str(output),
        working_directory=str(ROOT),
        pythonpath=str(ROOT) + ":" + str(ROOT / "sdk/python"),
        docker_binary=shutil.which("docker"),
        controller="harbor",
        harbor_version=importlib.metadata.version("harbor"),
    )
    if plan["harbor_version"] != "0.27.0":
        raise PermissionError("Install the pinned Harbor version")
    frozen = read(plan["index_content_file"])
    current = snapshot(plan["server_container"], selected_scopes(plan))
    if current != frozen:
        raise PermissionError("Source index changed since the original frozen snapshot")
    write(output / "index-content.json", current)
    plan["index_content_sha256"] = digest(current)
    plan["index_monitor"] = {
        "ignored_state_keys": sorted(MUTABLE_STATE_KEYS),
        "source_snapshot_matches_original": True,
        "reason": (
            "Exclude status caches and the memory-link worker's checked-head marker; "
            "source heads, units and unknown state keys remain fingerprinted."
        ),
    }
    plan["preparation"] = {
        "input_protocol_sha256": digest(original),
        "execution": (
            "Harbor native Docker agent environments and separate Docker verifiers; "
            "external factory agent."
        ),
        "unchanged": [
            "model_id",
            "schedule",
            "schedule_sha256",
            "tasks",
            "attempt_budget",
            "visible_test_call_limit",
        ],
        "timing": (
            "Harbor trial wall time includes setup, agent execution, artifact transfer, "
            "hidden verification and teardown. End-to-end also includes both index audits."
        ),
        "retry_policy": (
            "Harbor max_retries=0; direct inference and Loom SDK retries remain disabled."
        ),
    }
    assert all(plan[k] == original[k] for k in plan["preparation"]["unchanged"])
    plan["harbor_specs"], images = {}, {}
    for task_id in sorted(plan["tasks"]):
        plan["harbor_specs"][task_id], images[task_id] = load_profile(plan).stage_task(
            plan, task_id, output, build_image
        )
    plan["harbor_images"] = images
    write(output / "images.json", images)
    tasks = []
    for slot in plan["schedule"]:
        task_dir = output / "tasks" / f"slot-{slot['order']:02d}"
        task_dir.mkdir(parents=True)
        # Harbor clears job overlays for separate verifiers; their task definition owns it.
        stage_sandbox_policy(task_dir, verifier=True)
        _, _, task, _ = task_inputs(plan, slot["task_id"])
        write(
            task_dir / "instruction.md", {"schedule_order": slot["order"], "problem": task.problem}
        )
        image = images[slot["task_id"]]
        (task_dir / "task.toml").write_text(
            'schema_version = "1.4"\nartifacts = ["/logs/artifacts/candidate.json"]\n'
            "[agent]\ntimeout_sec = inf\n"
            "[environment]\ndocker_image = "
            + json.dumps(image["agent"])
            + '\ncpus = 1\nmemory_mb = 512\nnetwork_mode = "no-network"\n'
            '[verifier]\ntimeout_sec = 300\nenvironment_mode = "separate"\n'
            "[verifier.environment]\ndocker_image = "
            + json.dumps(image["verifier"])
            + '\ncpus = 1\nmemory_mb = 512\nnetwork_mode = "no-network"\n'
        )
        tasks.append({"path": str(task_dir)})
    protocol_path = output / "protocol.json"
    agent = {
        "import_path": "examples.software_factory.harbor.agent:FactoryAgent",
        "model_name": plan["model_id"],
        "kwargs": {"protocol_path": str(protocol_path)},
    }
    for name, control in (("live", None), ("base", "base"), ("reference", "reference")):
        selected = (
            tasks
            if control is None
            else [
                tasks[
                    next(
                        s["order"]
                        for s in plan["schedule"]
                        if s["task_id"] == t and s["mode"] == "baseline"
                    )
                ]
                for t in sorted(plan["tasks"])
            ]
        )
        config = JobConfig.model_validate(
            {
                "job_name": name,
                "jobs_dir": str(output / "jobs"),
                "n_attempts": 1,
                "n_concurrent_trials": 1,
                "retry": {"max_retries": 0},
                "environment": {
                    "type": "docker",
                    "delete": True,
                    "extra_docker_compose": [str(sandbox_policy)],
                },
                "agents": [
                    {
                        **agent,
                        "kwargs": {
                            **agent["kwargs"],
                            **({"qualification": control} if control else {}),
                        },
                    }
                ],
                "tasks": selected,
                "quiet": True,
            }
        )
        write(output / (name + "-job.json"), config.model_dump(mode="json"))
    paths = [
        p
        for p in PACKAGE_ROOT.rglob("*")
        if p.suffix in {".py", ".json", ".txt", ".html", ".js", ".yaml", ".in"}
        and "__pycache__" not in p.parts
    ]
    paths += list((ROOT / "sdk/python/loom").rglob("*.py"))
    if verified_input_files(plan) != inputs:
        raise PermissionError("Input identities changed during preparation")
    paths += list(inputs)
    paths += [Path(plan["historical_manifest"])]
    paths += [Path(c["evaluation_file"]) for c in plan["tasks"].values()]
    paths += [Path(plan["reference_controls_file"])]
    paths += [HERE / "sandbox-compose.yaml", sandbox_policy]
    paths += [p for p in (output / "tasks").rglob("*") if p.is_file()]
    paths += [output / (n + "-job.json") for n in ("live", "base", "reference")]
    paths += [output / "index-content.json", output / "images.json"]
    plan["files"] = {str(p): file_hash(p) for p in sorted(set(paths))}
    plan.pop("source_archive", None)
    write(protocol_path, plan)
    return protocol_path


def result_record(slot: dict, raw: dict, folder: Path, *, task_kind: str) -> dict:
    context = raw.get("agent_result") or {}
    result = (context.get("metadata") or {}).get("factory")
    if result is None and (folder / "agent/factory-result.json").exists():
        result = read(folder / "agent/factory-result.json")
    if result is None:
        result = {
            "task_id": slot["task_id"],
            "mode": slot["mode"],
            "arm": slot["arm"],
            "index_tier": slot["index_tier"],
            "kind": task_kind,
            "provider": "anthropic-messages:claude-haiku-5-5",
            "resolved": False,
            "generation_cost_usd": context.get("cost_usd"),
            "provider_usage": [],
            "cost_status": "unknown",
            "total_cost_usd": None,
        }
        if slot["mode"] == "baseline":
            result.update(
                {
                    k: 0
                    for k in (
                        "loom_tool_calls",
                        "model_selected_loom_requests",
                        "loom_logical_requests",
                        "loom_physical_requests",
                    )
                }
            )
    else:
        result = json.loads(json.dumps(result))
    grade_path = folder / "verifier/grade.json"
    result["hidden_grade"] = read(grade_path) if grade_path.exists() else None
    suite_path = folder / "verifier/full-suite.json"
    result["full_suite"] = read(suite_path) if suite_path.exists() else None
    rewards = (raw.get("verifier_result") or {}).get("rewards") or {}
    result["resolved"] = raw.get("reward_valid") is True and rewards.get("reward") == 1
    result["status"] = (
        "infrastructure_error" if not raw.get("reward_valid") else result.get("status", "completed")
    )
    result["harbor_exception"] = raw.get("exception_info")
    from datetime import datetime

    start, finish = (datetime.fromisoformat(raw[k]) for k in ("started_at", "finished_at"))
    result["end_to_end_s"] = (finish - start).total_seconds()
    result["harbor_wall_s"] = result["end_to_end_s"]
    measurement = folder / "factory-measurement.json"
    if measurement.exists():
        result.update(read(measurement))
    result["harbor_trial"] = str(folder)
    result["schedule_order"] = slot["order"]
    result["pairing_key"] = [slot["task_id"], slot["repeat"]]
    result["harbor_reward_valid"] = raw.get("reward_valid") is True
    return result


def enrich_evaluations(protocol, entries, job_name):
    profile = load_profile(protocol)
    base_path = Path(protocol["output_root"]) / "base-qualification.json"
    bases = {}
    if job_name != "base" and base_path.exists():
        receipt = read(base_path)
        if receipt.get("protocol_sha256") != digest(protocol):
            raise PermissionError("Full-suite baseline belongs to another protocol")
        bases = {
            entry["result"]["task_id"]: entry["result"].get("full_suite")
            for entry in receipt["attempts"]
        }
    verdicts = load_verdicts(protocol) if job_name == "live" else {}
    billing = {}
    bill_path = Path(protocol["output_root"]) / "provider-costs.json"
    if job_name == "live" and bill_path.exists():
        receipt = read(bill_path)
        if receipt.get("protocol_sha256") != digest(protocol):
            raise PermissionError("Provider costs belong to another protocol")
        billing = receipt["attempts"]
    for entry in entries:
        result = entry["result"]
        result.update(token_totals(result))
        billed = billing.get(str(entry["order"]), {})
        if billed.get("proof_status") != "known":
            billed = {}
        result.update({key: billed.get(key) for key in BILLING_FIELDS})
        result.update(paid_token_totals(result))
        components = [result.get("generation_cost_usd"), result.get("loom_provider_cost_usd")]
        result["total_provider_cost_usd"] = sum(components) if None not in components else None
        full = result.get("full_suite")
        changed = profile.full_suite_regressions(
            full if job_name == "base" else bases.get(result["task_id"]), full
        )
        result["full_suite_regressions"] = len(changed) if changed is not None else None
        result["full_suite_regression_tests"] = changed
        result["judge_fixed"] = None
        judged = verdicts.get(str(entry["order"]))
        state = (result.get("hidden_grade") or {}).get("state")
        if judged and state and judged["candidate_state"] == state:
            result["judge_fixed"] = judged["judge_fixed"]


def export(protocol: dict, job_name="live") -> dict:
    output = Path(protocol["output_root"])
    entries = []
    for folder in sorted((output / "jobs" / job_name).glob("*")):
        path = folder / "result.json"
        if not path.exists() or not (folder / "config.json").exists():
            continue
        raw = read(path)
        order = int(Path(raw["config"]["task"]["path"]).name.removeprefix("slot-"))
        result = result_record(
            protocol["schedule"][order], raw, folder, task_kind=load_profile(protocol).TASK_KIND
        )
        entries.append({"order": order, "result": result})
    entries.sort(key=lambda e: e["order"])
    enrich_evaluations(protocol, entries, job_name)
    write(output / (job_name + "-attempts.json"), entries)
    if job_name != "live":
        return {
            "attempts": entries,
            "protocol_sha256": digest(protocol),
            "qualified": len(entries) == len(protocol["tasks"])
            and all(
                e["result"]["harbor_reward_valid"]
                and e["result"]["resolved"] == (job_name == "reference")
                and e["result"].get("provider_physical_requests") == 0
                and (e["result"].get("full_suite") or {}).get("valid") is True
                and (job_name != "reference" or e["result"]["full_suite_regressions"] == 0)
                for e in entries
            ),
        }
    report = analyze(
        {"attempts": protocol["schedule"], "schedule_sha256": protocol["schedule_sha256"]}, entries
    )
    report.update(analyze_cohorts(protocol, entries))
    write(output / "paired-report.json", report)
    summary = {
        "complete": report["complete"],
        "observed": len(entries),
        "scheduled": len(protocol["schedule"]),
        "arms": {},
    }
    for arm in ARMS:
        records = [e["result"] for e in entries if protocol["schedule"][e["order"]]["arm"] == arm]
        costs = [r.get("generation_cost_usd") for r in records]
        summary["arms"][arm] = {
            "attempts": len(records),
            "resolved": sum(r["resolved"] for r in records),
            "infrastructure_errors": sum(not r["harbor_reward_valid"] for r in records),
            "mean_end_to_end_s": fmean(r["end_to_end_s"] for r in records) if records else None,
            "generation_cost_usd": sum(costs) if records and None not in costs else None,
            "model_selected_loom_calls": known_total(records, "model_selected_loom_requests"),
            **{
                key: known_total(records, key)
                for key in (
                    "agent_time_s",
                    "agent_test_requests",
                    "agent_test_runs",
                    "completion_reviews",
                    "loom_tool_calls",
                    "memory_rows_written",
                    "full_suite_regressions",
                    "judge_fixed",
                    "loom_provider_cost_usd",
                    "total_provider_cost_usd",
                    "model_input_tokens",
                    "model_output_tokens",
                    "model_total_tokens",
                    "model_cache_read_tokens",
                    "model_cache_write_tokens",
                )
            },
            "unknown_loom_counts": sum(r.get("loom_physical_requests") is None for r in records),
        }
    write(output / "summary.json", summary)
    if entries:
        columns = (
            "order",
            "task_id",
            "arm",
            "repeat",
            "resolved",
            "status",
            "end_to_end_s",
            "duration_s",
            "generation_cost_usd",
            "loom_provider_cost_usd",
            "total_provider_cost_usd",
            "total_cost_usd",
            "agent_time_s",
            "agent_test_requests",
            "agent_test_runs",
            "completion_reviews",
            "loom_tool_calls",
            "memory_rows_written",
            "full_suite_regressions",
            "judge_fixed",
            "model_input_tokens",
            "model_output_tokens",
            "model_total_tokens",
            "model_cache_read_tokens",
            "model_cache_write_tokens",
            "model_selected_loom_requests",
            "loom_physical_requests",
            "provider_physical_requests",
        )
        with (output / "attempts.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for entry in entries:
                merged = {**entry["result"], **protocol["schedule"][entry["order"]]}
                writer.writerow({k: merged.get(k) for k in columns})
    summary["reporting"] = generate_reports(protocol, entries, job_name=job_name)
    audit_path = output / "fairness-audit.json"
    summary["fairness_verified"] = read(audit_path).get("passed") if audit_path.exists() else None
    if protocol.get("public_reproduction"):
        summary["public_export"] = export_public(protocol, output / "public-results")
    write(output / "summary.json", summary)
    return summary


async def run(protocol_path: Path, kind: str, *, meter=None):
    protocol = read(protocol_path)
    verify(protocol)
    output = Path(protocol["output_root"])
    if (output / "jobs" / kind).exists():
        raise PermissionError(
            "Job already exists; reconcile interruptions instead of replaying trials"
        )
    if kind == "live":
        verify_qualifications(protocol)
    config = JobConfig.model_validate(read(output / (kind + "-job.json")))
    job = await Job.create(config)
    if config.n_concurrent_trials != 1 or config.retry.max_retries != 0:
        raise PermissionError("Frozen trial order requires serial execution without retries")
    timers = {}
    frozen_snapshot = read(output / "index-content.json")
    next_orders = [int(Path(t.path).name.removeprefix("slot-")) for t in config.tasks]

    async def start(event):
        timers[event.trial_name] = time.monotonic()
        verify(protocol)
        order = int(event.config.task.path.name.removeprefix("slot-"))
        if meter is not None:
            if meter.blocked:
                raise PermissionError("Unsettled provider billing stops the cohort")
            meter.set_phase(f"{kind}:{order}")
        if not next_orders or order != next_orders.pop(0):
            raise PermissionError("Harbor changed the frozen trial order")
        scopes = selected_scopes(protocol, protocol["schedule"][order]) if kind == "live" else []
        current = await asyncio.to_thread(snapshot, protocol["server_container"], scopes)
        write(output / f"{kind}-index-before-{order:02d}.json", current)
        if current != select_snapshot(frozen_snapshot, scopes):
            raise PermissionError("Source index changed before the Harbor trial")
        intent = output / f"{kind}-intent-{order:02d}.json"
        with intent.open("x") as stream:
            json.dump(
                {
                    "slot": protocol["schedule"][order],
                    "trial_name": event.trial_name,
                    "protocol_sha256": digest(protocol),
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())

    async def end(event):
        order = int(event.config.task.path.name.removeprefix("slot-"))
        scopes = selected_scopes(protocol, protocol["schedule"][order]) if kind == "live" else []
        current = await asyncio.to_thread(snapshot, protocol["server_container"], scopes)
        write(output / f"{kind}-index-after-{order:02d}.json", current)
        if current != select_snapshot(frozen_snapshot, scopes):
            raise PermissionError("Source index changed during the Harbor trial")
        trial_folder = event.config.trials_dir / event.trial_name
        write(
            trial_folder / "factory-measurement.json",
            {
                "end_to_end_s": time.monotonic() - timers.pop(event.trial_name),
                "harbor_wall_s": (
                    event.result.finished_at - event.result.started_at
                ).total_seconds(),
            },
        )
        summary = export(protocol, kind)
        print(
            json.dumps(
                {
                    "job": kind,
                    "order": order,
                    "arm": protocol["schedule"][order]["arm"],
                    "reward_valid": event.result.reward_valid,
                    "reward": event.result.verifier_result.rewards
                    if event.result.verifier_result
                    else None,
                    "observed": summary.get("observed", len(summary.get("attempts", []))),
                }
            ),
            flush=True,
        )

    job.on_trial_started(start).on_trial_ended(end)
    if kind == "live":
        export(protocol, kind)
    await job.run()
    summary = export(protocol, kind)
    if kind == "live":
        costs = await asyncio.to_thread(
            audit_provider_costs, protocol, read(output / "live-attempts.json")
        )
        await asyncio.to_thread(
            audit_fairness, protocol, read(output / "live-attempts.json"), billing=costs
        )
        export(protocol)
        if meter is not None:
            meter.set_phase("judge")
        await asyncio.to_thread(
            run_judging,
            protocol,
            read(output / "live-attempts.json"),
            on_update=lambda: export(protocol),
        )
        summary = export(protocol)
    if kind != "live":
        write(output / (kind + "-qualification.json"), summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare", "base", "reference", "live", "report", "audit", "judge")
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.output is None:
            parser.error("prepare requires --output")
        print(prepare(args.protocol.resolve(), args.output.resolve()))
    elif args.command == "report":
        print(json.dumps(export(read(args.protocol)), indent=2))
    elif args.command in {"audit", "judge"}:
        protocol = read(args.protocol)
        verify(protocol)
        entries = read(Path(protocol["output_root"]) / "live-attempts.json")
        if args.command == "audit":
            costs = audit_provider_costs(protocol, entries)
            audit_fairness(protocol, entries, billing=costs)
        else:
            run_judging(protocol, entries, on_update=lambda: export(protocol))
        print(json.dumps(export(protocol), indent=2))
    else:
        print(json.dumps(asyncio.run(run(args.protocol.resolve(), args.command)), indent=2))


if __name__ == "__main__":
    main()
