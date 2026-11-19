"""Run and externally grade one issue-resolution arm using the installed Python SDK."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bench.agent_traj.claude_code_driver import run_process_group
from bench.sdk_issue_agent import (
    POLICY,
    agent_loop,
    append_event,
    error_detail,
    guard_refused,
    local_path,
    tools,
)

TASK_FIELDS = {
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "environment_setup_commit",
    "created_at",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "--no-pager", "-C", str(root), *args],
        stderr=subprocess.PIPE,
        timeout=15,
    )


def read_task(path: Path, expected: str) -> dict:
    task = json.loads(path.read_text())
    if (
        not isinstance(task, dict)
        or set(task) - TASK_FIELDS
        or any(not isinstance(value, str) for value in task.values())
    ):
        raise ValueError("Task must contain only sanitized public task fields")
    if not all(
        isinstance(task.get(k), str) and task[k].strip()
        for k in ("instance_id", "repo", "base_commit", "problem_statement")
    ):
        raise ValueError("Task requires instance_id, repo, base_commit and problem_statement")
    if task["base_commit"] != expected:
        raise ValueError("Task base differs from expected base")
    return task


def read_grader(path: Path) -> dict:
    grader = json.loads(path.read_text())
    if set(grader) != {"command", "selected_tests", "timeout_s"}:
        raise ValueError("Grader requires command, selected_tests and timeout_s")
    if (
        not isinstance(grader["command"], list)
        or not grader["command"]
        or not all(isinstance(x, str) for x in grader["command"])
    ):
        raise ValueError("Grader command must be an argument list")
    if (
        not isinstance(grader["selected_tests"], list)
        or not grader["selected_tests"]
        or not all(isinstance(x, str) for x in grader["selected_tests"])
    ):
        raise ValueError("Declare the external grader's identical selected tests")
    if not isinstance(grader["timeout_s"], (int, float)) or grader["timeout_s"] <= 0:
        raise ValueError("Grader timeout must be positive")
    if not all(any(field in arg for arg in grader["command"]) for field in ("{patch}", "{output}")):
        raise ValueError("Grader command must consume {patch} and write {output}")
    return grader


def manifest_for(args: argparse.Namespace) -> dict:
    root = args.checkout.resolve()
    url = urlsplit(args.base_url)
    if args.arm == "loom" and (url.scheme not in ("http", "https") or not url.netloc):
        raise ValueError("Loom arm requires an explicit HTTP server URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Server URL cannot contain credentials, query or fragment")
    if args.out.resolve().is_relative_to(root) or args.grader_config.resolve().is_relative_to(root):
        raise PermissionError(
            "Run artifacts and private grader configuration must be outside checkout"
        )
    actual = git(root, "rev-parse", "HEAD").decode().strip()
    if actual != args.expected_base or git(root, "status", "--porcelain", "--untracked-files=all"):
        raise PermissionError("A clean checkout at the exact expected base is required")
    task = read_task(args.task, args.expected_base)
    grader = read_grader(args.grader_config)
    versions = {}
    for package in ("boto3", "clickhouse-loom"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    protocol = {
        "model": args.model,
        "region": args.region,
        "system": POLICY + f" Repository: {task['repo']} at {args.expected_base}.",
        "instruction_profile": "conditional-index-first-v1",
        "tool_order_profile": "indexed-lookup-first-when-present",
        "max_turns": args.max_turns,
        "timeout_s": args.timeout_s,
        "inference_config": {"maxTokens": args.max_tokens},
        "task_sha256": digest(task),
        "expected_base": args.expected_base,
        "grader_sha256": digest(grader),
        "versions": versions,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "agent_sha256": hashlib.sha256(
            Path(agent_loop.__code__.co_filename).read_bytes()
        ).hexdigest(),
    }
    return {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "arm": args.arm,
        "task": task,
        "checkout": str(root),
        "protocol": protocol,
        "protocol_sha256": digest(protocol),
        "tools": tools(args.arm),
        "scope": {"org": args.org, "namespace": args.namespace},
        "connection": {"base_url": args.base_url, "token_env": args.token_env},
        "grader": grader,
        "frozen_at_unix": time.time(),
    }


def events_in(out: Path) -> list[dict]:
    rows = []
    if (out / "events.jsonl").exists():
        for line in (out / "events.jsonl").read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass  # A killed writer may leave its final append incomplete.
    return rows


def collect_patch(root: Path, out: Path, events: list[dict]) -> list[str]:
    chunks, errors = [], []
    try:
        chunks.append(git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"))
        paths = set(
            git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
        )
        paths.update(
            e["result"]["content"][0]["json"]["path"]
            for e in events
            if e.get("type") == "tool_result"
            and e.get("status") == "success"
            and e.get("name") == "replace_text"
            and e["result"]["content"][0]["json"].get("created")
        )
        for name in sorted(paths - {""}):
            path = local_path(root, name)
            if not path.is_file():
                continue
            proc = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                    "/dev/null",
                    name,
                ],
                cwd=root,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode not in (0, 1):
                raise RuntimeError("Could not preserve a new-file patch")
            chunks.append(proc.stdout)
    except Exception as error:  # noqa: BLE001 — preserve every patch fragment on failure
        errors.append(type(error).__name__)
    (out / "agent.diff").write_bytes(b"".join(chunks))
    return errors


def recorded_refusal(events: list[dict]) -> dict | None:
    """The first persisted refusal, with the progress known at that event."""
    progress = {}
    for event in events:
        if event.get("type") == "worker_stage":
            progress = event
        error = event.get("error")
        denied_error = (
            event.get("type")
            in {"terminal", "tool_result", "provider_cleanup_error", "sdk_cleanup_error"}
            and isinstance(error, dict)
            and (
                error.get("http_status") in (401, 403, 409)
                or error.get("type") == "PermissionError"
            )
        )
        if event.get("status") == "guard_refused" or denied_error:
            refusal = {**progress, **event, "status": "guard_refused"}
            if event.get("type") == "tool_result":
                refusal.update(stage="agent_inference", agent_inference_started=True)
            return refusal
    return None


def finalize(
    out: Path, root: Path, manifest: dict, outcome: dict, started: float, *, setup_validated: bool
) -> dict:
    events = events_in(out)
    if setup_validated:
        patch_errors = collect_patch(root, out, events)
    else:
        (out / "agent.diff").write_bytes(b"")
        patch_errors = []
    usage: Counter = Counter()
    for event in events:
        if event.get("type") == "model_response":
            usage.update(
                {
                    k: v
                    for k, v in event["response"].get("usage", {}).items()
                    if isinstance(v, (int, float))
                }
            )
    terminal = next((e for e in reversed(events) if e.get("type") == "terminal"), {})
    supervision = dict(outcome)
    refusal = recorded_refusal(events)
    if refusal is not None:
        outcome = refusal
    elif outcome.get("status") == "worker_exited":
        outcome = (
            terminal
            if terminal and outcome.get("returncode") == 0
            else {**outcome, "status": "worker_error", "worker_terminal": terminal}
        )
    patch = (out / "agent.diff").read_bytes()
    status = outcome["status"]
    if status == "completed" and not patch:
        status = "completed_empty_patch"
    if patch_errors and status != "guard_refused":
        status = "artifact_error"
    calls = [e for e in events if e.get("type") == "tool_result"]
    attempts = [e for e in events if e.get("type") == "tool_attempt"]
    sdk_calls = [e for e in calls if e["name"] == "loom_code_lookup"]
    sdk_unfinished = sum(e["name"] == "loom_code_lookup" for e in attempts) - len(sdk_calls)
    sdk_completed_wall_s = sum(e.get("duration_s", 0) for e in sdk_calls)
    model_responses = [e for e in events if e.get("type") == "model_response"]
    progress = next((e for e in reversed(events) if e.get("type") == "worker_stage"), {})
    preflight_status = outcome.get("preflight_status", progress.get("preflight_status"))
    result = {
        "schema_version": 1,
        "arm": manifest.get("arm"),
        "instance_id": manifest.get("task", {}).get("instance_id"),
        "protocol_sha256": manifest.get("protocol_sha256"),
        "status": status,
        "supervision": supervision,
        "answer": outcome.get("answer", ""),
        "error": outcome.get("error"),
        "stage": outcome.get(
            "stage", progress.get("stage", "setup" if not setup_validated else None)
        ),
        "preflight_status": preflight_status,
        "preflight_failed": preflight_status == "failed" if preflight_status is not None else None,
        "agent_inference_started": outcome.get(
            "agent_inference_started",
            progress.get(
                "agent_inference_started",
                True if model_responses else None if setup_validated else False,
            ),
        ),
        "duration_s": round(time.monotonic() - started, 3),
        "usage": dict(usage),
        "usage_complete": status
        in {"completed", "completed_empty_patch", "max_turns", "model_limit"}
        and all(isinstance(e["response"].get("usage"), dict) for e in model_responses),
        "tool_calls": len(attempts),
        "unfinished_tool_calls": len(attempts) - len(calls),
        "failed_tool_calls": sum(e["status"] == "error" for e in calls),
        "model_selected_loom_calls": sum(e["name"] == "loom_code_lookup" for e in attempts),
        "model_usage": {manifest.get("protocol", {}).get("model", ""): dict(usage)},
        "sdk_lookup_wall_s": None if sdk_unfinished else sdk_completed_wall_s,
        "sdk_lookup_timing_complete": sdk_unfinished == 0,
        "sdk_completed_lookup_wall_s": sdk_completed_wall_s,
        "sdk_unfinished_lookups": sdk_unfinished,
        "sdk_failed_lookups": sum(
            e["name"] == "loom_code_lookup" and e["status"] == "error" for e in calls
        ),
        "loom_results": [e for e in events if e.get("type") == "loom_sdk_result"],
        "patch_bytes": len(patch),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_errors": patch_errors,
        "resolved": None,
    }
    write_json(out / "RESULT.json", result)
    return result


def run_trial(args: argparse.Namespace) -> dict:
    out = args.out.resolve()
    if out.is_relative_to(args.checkout.resolve()):
        raise PermissionError("Run directory must be outside checkout")
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    started, manifest = time.monotonic(), {}
    setup_validated = False
    outcome = {"status": "invalid_setup"}
    try:
        manifest = manifest_for(args)
        setup_validated = True
        write_json(out / "MANIFEST.json", manifest)
        with (out / "stdout.txt").open("w") as stdout, (out / "stderr.txt").open("w") as stderr:
            code, timed_out = run_process_group(
                [
                    sys.executable,
                    "-m",
                    "bench.sdk_issue_resolution",
                    "worker",
                    "--run-dir",
                    str(out),
                ],
                cwd=Path.cwd(),
                env=dict(os.environ),
                stdout=stdout,
                stderr=stderr,
                timeout_s=args.timeout_s,
            )
        outcome = {"status": "timeout" if timed_out else "worker_exited", "returncode": code}
    except PermissionError as error:
        outcome = {"status": "guard_refused", "error": error_detail(error)}
    except (ValueError, FileNotFoundError) as error:
        outcome = {"status": "invalid_setup", "error": error_detail(error)}
    except KeyboardInterrupt as error:
        outcome = {"status": "interrupted", "error": error_detail(error)}
    except Exception as error:  # noqa: BLE001 — setup and supervision failures still get RESULT
        outcome = {"status": "runner_error", "error": error_detail(error)}
    finally:
        result = finalize(
            out,
            args.checkout.resolve(),
            manifest,
            outcome,
            started,
            setup_validated=setup_validated,
        )
    return result


def _sdk_from_manifest(manifest: dict) -> Any:
    from loom import Agent

    connection, scope = manifest["connection"], manifest["scope"]
    token = os.environ.get(connection["token_env"], "")
    if not token:
        raise PermissionError("Explicit Loom token environment variable is required")
    consumer = "sdk-issue-" + manifest["run_id"]
    return Agent(
        **scope,
        agent=consumer,
        base_url=connection["base_url"],
        api_key=token,
        consumer=consumer,
        mentor_brief=False,
        auto_run=False,
        pacing=False,
        ratelimit_max_wait_s=0,
        retry={"max_attempts": 1},
    )


async def _sdk_preflight(sdk: Any, manifest: dict, record: Any) -> None:
    started = time.monotonic()
    preflight = await sdk.call_with_meta("code.index_status", manifest["scope"])
    record(
        {
            "type": "loom_preflight",
            "request_id": preflight.request_id,
            "data": preflight.data,
            "duration_s": time.monotonic() - started,
        }
    )
    if preflight.data.get("git_head") != manifest["protocol"]["expected_base"]:
        raise PermissionError("Index does not match the task base")


def _cleanup_error(record: Any, outcome: dict, progress: dict, kind: str, error: Exception) -> None:
    cleanup = {
        **progress,
        "stage": kind.removesuffix("_error"),
        "status": "guard_refused" if guard_refused(error) else "cleanup_error",
        "error": error_detail(error),
    }
    if outcome.get("status") == "guard_refused":
        record({"type": kind, **cleanup})
    else:
        outcome.update(cleanup)
        record({"type": "terminal", **outcome})


async def worker(out: Path) -> None:
    manifest = json.loads((out / "MANIFEST.json").read_text())
    record = partial(append_event, out / "events.jsonl")
    sdk = None
    client = None
    outcome: dict = {}
    progress = {
        "stage": "source_validation",
        "preflight_status": "not_started" if manifest["arm"] == "loom" else "not_requested",
        "agent_inference_started": False,
    }
    try:
        record({"type": "worker_stage", **progress})
        hashes = {
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "agent_sha256": hashlib.sha256(
                Path(agent_loop.__code__.co_filename).read_bytes()
            ).hexdigest(),
        }
        if any(manifest["protocol"][key] != value for key, value in hashes.items()):
            raise PermissionError("Worker source differs from the frozen manifest")
        progress["stage"] = "provider_setup"
        import boto3
        from botocore.config import Config

        if manifest["arm"] == "loom":
            progress["stage"] = "sdk_setup"
            sdk = _sdk_from_manifest(manifest)
            await sdk.__aenter__()
            progress.update(stage="loom_preflight", preflight_status="started")
            record({"type": "worker_stage", **progress})
            await _sdk_preflight(sdk, manifest, record)
            progress["preflight_status"] = "passed"
        progress["stage"] = "provider_setup"
        client = boto3.client(
            "bedrock-runtime",
            region_name=manifest["protocol"]["region"],
            config=Config(connect_timeout=5, read_timeout=45, retries={"total_max_attempts": 1}),
        )
        progress.update(stage="agent_inference", agent_inference_started=True)
        record({"type": "worker_stage", **progress})
        outcome = await agent_loop(Path(manifest["checkout"]), manifest, client, sdk, record)
        record({"type": "terminal", **outcome, **progress})
    except Exception as error:  # noqa: BLE001 — retain protocol/provider/guard failures for grading
        if progress["preflight_status"] == "started":
            progress["preflight_status"] = "failed"
        outcome = {
            "status": "guard_refused" if guard_refused(error) else "provider_error",
            "error": error_detail(error),
            **progress,
        }
        record({"type": "terminal", **outcome})
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:  # noqa: BLE001 — cleanup must retain an earlier guard refusal
                _cleanup_error(record, outcome, progress, "provider_cleanup_error", error)
        if sdk is not None:
            try:
                await sdk.__aexit__(None, None, None)
            except Exception as error:  # noqa: BLE001 — cleanup failure cannot look completed
                _cleanup_error(record, outcome, progress, "sdk_cleanup_error", error)


def grade_outcome(report: Any, selected: list[str], code: int | None, timed_out: bool) -> dict:
    invalid = {"status": "invalid_setup", "valid": False, "resolved": None}
    if timed_out or code != 0 or not isinstance(report, dict):
        return {**invalid, "reason": "Grader failed or structured report is missing"}
    statuses = report.get("selected_test_statuses")
    if (
        not isinstance(report.get("valid"), bool)
        or not isinstance(report.get("resolved"), bool)
        or not isinstance(statuses, dict)
        or not isinstance(report.get("missing_tests"), list)
    ):
        return {**invalid, "reason": "Incomplete grader report"}
    if (
        not report["valid"]
        or report["missing_tests"]
        or any(statuses.get(case) not in ("PASSED", "FAILED", "ERROR") for case in selected)
    ):
        return {**invalid, "reason": "Invalid grading or missing selected-test outcomes"}
    if report["resolved"] != all(statuses[case] == "PASSED" for case in selected):
        return {**invalid, "reason": "Resolved conflicts with selected-test outcomes"}
    return {"status": "graded", "valid": True, "resolved": report["resolved"]}


def grade_trial(out: Path) -> dict:
    manifest = json.loads((out / "MANIFEST.json").read_text())
    result = json.loads((out / "RESULT.json").read_text())
    grader = manifest["grader"]
    if digest(grader) != manifest["protocol"]["grader_sha256"]:
        raise PermissionError("Frozen grader configuration changed")
    if hashlib.sha256((out / "agent.diff").read_bytes()).hexdigest() != result["patch_sha256"]:
        raise PermissionError("Saved patch changed")
    dest = out / "grading"
    dest.mkdir(mode=0o700, exist_ok=False)
    fields = {
        "patch": str(out / "agent.diff"),
        "output": str(dest / "result.json"),
        "run": str(out),
        "arm": manifest["arm"],
        "instance_id": manifest["task"]["instance_id"],
    }
    code, timed_out, report = None, False, None
    error = None
    try:
        command = [arg.format(**fields) for arg in grader["command"]]
        with (dest / "stdout.txt").open("w") as stdout, (dest / "stderr.txt").open("w") as stderr:
            code, timed_out = run_process_group(
                command,
                cwd=dest,
                env=dict(os.environ),
                stdout=stdout,
                stderr=stderr,
                timeout_s=grader["timeout_s"],
            )
        if (dest / "result.json").exists():
            report = json.loads((dest / "result.json").read_text())
    except Exception as exc:  # noqa: BLE001 — a failed grader remains an attempted run
        error = error_detail(exc)
    finally:
        graded = {
            "grader_sha256": digest(grader),
            "returncode": code,
            "timed_out": timed_out,
            "result": report,
            "error": error,
            **grade_outcome(report, grader["selected_tests"], code, timed_out),
        }
        write_json(out / "GRADE.json", graded)
    return graded


def compare_runs(baseline: Path, treatment: Path) -> dict:
    manifests = [json.loads((p / "MANIFEST.json").read_text()) for p in (baseline, treatment)]
    if [m["arm"] for m in manifests] != ["baseline", "loom"]:
        raise ValueError("Comparison requires baseline and loom arms in that order")
    if manifests[0]["protocol"] != manifests[1]["protocol"]:
        raise ValueError("Arms differ in task, model, instructions, limits, grader or runner")
    if any(digest(m["protocol"]) != m["protocol_sha256"] for m in manifests):
        raise ValueError("Frozen protocol hash does not match manifest contents")
    if any(m["tools"] != tools(m["arm"]) for m in manifests):
        raise ValueError("Arms differ from the declared local-tools-plus-binding protocol")
    return {
        "protocol_sha256": manifests[0]["protocol_sha256"],
        "arms": {
            m["arm"]: {
                "agent": json.loads((p / "RESULT.json").read_text()),
                "grade": json.loads((p / "GRADE.json").read_text())
                if (p / "GRADE.json").exists()
                else {
                    "status": "invalid_setup",
                    "valid": False,
                    "resolved": None,
                    "reason": "No external grade recorded",
                },
            }
            for p, m in zip((baseline, treatment), manifests, strict=True)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one arm; model calls spend provider money")
    for name in ("task", "checkout", "grader-config", "out"):
        run.add_argument("--" + name, type=Path, required=True)
    for name in ("expected-base", "model", "org", "namespace"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--arm", choices=("baseline", "loom"), required=True)
    run.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    run.add_argument("--token-env", default="LOOM_API_KEY")
    run.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    run.add_argument("--timeout-s", type=float, default=110)
    run.add_argument("--max-turns", type=int, default=12)
    run.add_argument("--max-tokens", type=int, default=4096)
    for command in ("worker", "grade"):
        commands.add_parser(command).add_argument("--run-dir", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--loom", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "worker":
        asyncio.run(worker(args.run_dir.resolve()))
        return 0
    if args.command == "run":
        if min(args.timeout_s, args.max_turns, args.max_tokens) <= 0:
            parser.error("Time, turn and token limits must be positive")
        url = urlsplit(args.base_url)
        if url.username or url.password or url.query or url.fragment:
            parser.error("Server URL must not contain credentials, query or fragment")
        result = run_trial(args)
    elif args.command == "grade":
        result = grade_trial(args.run_dir.resolve())
    else:
        result = compare_runs(args.baseline, args.loom)
    print(json.dumps(result, indent=2))
    if args.command == "grade":
        return 0 if result["valid"] else 2
    return (
        0
        if args.command != "run" or result["status"] in ("completed", "completed_empty_patch")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
