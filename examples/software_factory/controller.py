"""Connect cold attempts, durable reservations and trusted process supervision.

The host supplies frozen worker commands and eligibility checks. Neither is an
agent capability. This module constructs no clients and grants no authorization.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

from .cohort import Cohort, ReservedProvider
from .engine import Factory
from .live_support import TransportAudit
from .model import Budget, digest
from .supervision import run_bounded
from .workspace import require_independent_grader


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_active(
    root: Path,
    protocol: dict,
    order: int,
    output: Path,
    *,
    verify_eligibility,
    prepare,
    provider_factory,
    context_factory=None,
) -> dict:
    """Run inside the supervised worker; construct clients after eligibility.

    prepare(task_id, output) returns the pinned Task and independent grader.
    provider_factory(output, audit) and context_factory(task, output, audit) return dedicated
    adapters. Baseline never invokes the context factory. Worker output and the
    cohort journal stay outside the agent's source-only checkout.
    """
    started = time.monotonic()
    with Cohort(root, protocol) as cohort:
        active = [i for i, row in enumerate(cohort.state["attempts"]) if row["state"] == "running"]
        if active != [order]:
            raise PermissionError("Worker does not own the active scheduled attempt")
        verify_eligibility(protocol)
        slot = protocol["schedule"][order]
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        task, grader = prepare(slot["task_id"], output)
        frozen = protocol["tasks"][slot["task_id"]]
        actual = {
            "task_id": task.task_id,
            "kind": task.kind,
            "repository": task.repository,
            "base_commit": task.base_commit,
            "namespace": task.namespace,
            "source_sha256": task.source_sha256,
            "grader_sha256": grader.sha256,
            "problem_sha256": digest(task.problem),
            "symbol": task.symbol,
        }
        if actual != frozen or digest(task.files) != task.source_sha256:
            raise PermissionError("Prepared task or independent grader differs from the protocol")
        require_independent_grader(grader)
        with ExitStack() as resources:
            provider_audit = TransportAudit(record=lambda row: cohort.transport("provider", row))
            provider = provider_factory(output, provider_audit)
            if hasattr(provider, "close"):
                resources.callback(provider.close)
            if provider.label != protocol["provider_label"]:
                raise PermissionError("Provider differs from the frozen protocol")
            backend = None
            if slot["mode"] != "baseline":
                if context_factory is None:
                    raise PermissionError("Context arm requires a qualified dedicated adapter")
                context_audit = TransportAudit(record=lambda row: cohort.transport("loom", row))
                backend = context_factory(task, output, context_audit)
                resources.callback(backend.close)
            result = Factory(
                task,
                ReservedProvider(
                    provider, cohort, verified_input_bound=protocol["verified_input_bound"]
                ),
                grader,
                output / "agent",
                mode=slot["mode"],
                backend=backend,
                budget=Budget(**protocol["attempt_budget"]),
                org=protocol["org"],
            ).run()
        # Export only after adapter cleanup; an interrupted cleanup is not a completed worker.
        export = {
            "order": order,
            "protocol_sha256": cohort.sha256,
            "worker_duration_s": time.monotonic() - started,
            "result": result,
        }
        _write(output / "completed.json", export)
        return export


def run_next(
    root: Path,
    protocol: dict,
    *,
    verify_eligibility,
    worker_command,
    cwd: Path,
    env: dict[str, str],
) -> dict | None:
    """Supervise one frozen worker, retaining timeout/failure without retrying it.

    worker_command(slot, worker_output) returns a trusted controller command,
    never candidate code. The separate lock spans the child lifetime while the
    cohort journal lock transfers to the worker for pre-dispatch reservations.
    Unknown usage after interruption blocks the following attempt automatically.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with Cohort(root, protocol) as cohort:
            slot = cohort.begin(verify_eligibility)
        if slot is None:
            return None
        output = root / f"attempt-{slot['order']:03d}"
        worker = output / "worker"
        halted = False
        try:
            supervision = run_bounded(
                worker_command(slot, worker),
                cwd=cwd,
                env=env,
                output=output,
                timeout_s=protocol["controller_timeout_s"],
            )
            completed = json.loads((worker / "completed.json").read_text())
            result = completed["result"]
            halted = result["status"] == "invalid_grader"
            if (
                supervision["timed_out"]
                or supervision["exit_code"] != 0
                or completed["order"] != slot["order"]
                or completed["protocol_sha256"] != digest(protocol)
                or result["task_id"] != slot["task_id"]
                or result["mode"] != slot["mode"]
                or result["provider"] != protocol["provider_label"]
            ):
                raise ValueError("Worker completion does not match the successful supervised slot")
        except Exception as error:  # noqa: BLE001 — Failed attempts remain scheduled results.
            halted = True
            result = {
                "task_id": slot["task_id"],
                "mode": slot["mode"],
                "kind": protocol["tasks"][slot["task_id"]]["kind"],
                "provider": protocol["provider_label"],
                "resolved": False,
                "status": "controller_failure",
                "error_type": type(error).__name__,
                "total_cost_usd": None,
                "cost_status": "unknown",
                "benchmark_claims": False,
            }
        result = {**result, "end_to_end_s": time.monotonic() - started}
        entry = {"order": slot["order"], "result": result}
        with Cohort(root, protocol) as cohort:
            cohort.finish(result, halted=halted)
        # Retain this export outside the candidate tree, even for failed workers.
        _write(root / f"result-{slot['order']:03d}.json", entry)
        return entry
