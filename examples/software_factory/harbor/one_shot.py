"""Run a repository profile through shared setup, qualification and measurement."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import sys
import traceback
from pathlib import Path

from .catalog import load_evaluation
from .gateway import Gateway
from .launch import native_model
from .profiles import load_profile
from .provision import Provisioner
from .runtime import read, write
from .spending import Spending


class Phases:
    def __init__(self, output: Path, owner: str):
        self.output, self.owner = output, owner
        self.path = output / "one-shot.jsonl"
        self.started, self.completed = set(), {}
        if self.path.exists():
            for line in self.path.read_text().splitlines(keepends=True):
                if not line.endswith("\n"):
                    raise PermissionError("Interrupted phase journal requires reconciliation")
                row = json.loads(line)
                if row["owner"] != owner:
                    raise PermissionError("Phase journal belongs to another run")
                if row["event"] == "started":
                    self.started.add(row["phase"])
                elif row["event"] == "completed":
                    self.completed[row["phase"]] = row

    def append(self, event, phase, **fields):
        row = {"owner": self.owner, "event": event, "phase": phase, **fields}
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def run(self, phase, function):
        if phase in self.completed:
            row = self.completed[phase]
            path = Path(row["receipt"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise PermissionError("A completed setup receipt changed")
            return read(path)
        if phase in self.started:
            raise PermissionError("An interrupted phase must be reconciled before it can run again")
        self.append("started", phase)
        self.started.add(phase)
        print(json.dumps({"phase": phase, "state": "started"}), flush=True)
        try:
            result = function()
        except Exception as error:  # persist the failure site without exception payloads
            self.append(
                "failed",
                phase,
                error_type=type(error).__name__,
                frames=[
                    {"file": Path(frame.filename).name, "line": frame.lineno}
                    for frame in traceback.extract_tb(error.__traceback__)
                ],
            )
            raise
        receipt = self.output / "phases" / (phase + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        write(receipt, result)
        self.append(
            "completed",
            phase,
            receipt=str(receipt),
            sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        )
        self.completed[phase] = {
            "receipt": str(receipt),
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        }
        print(json.dumps({"phase": phase, "state": "completed"}), flush=True)
        return result


def profile_for(source_root: Path, identifier: str):
    descriptor = load_evaluation(source_root, identifier)
    return load_profile({"suite": {"adapter": descriptor["adapter"]}})


def protocol_receipt(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def checked_protocol_path(receipt: dict) -> Path:
    path = Path(receipt["path"])
    if path.is_symlink() or protocol_receipt(path) != receipt:
        raise PermissionError("A frozen protocol changed")
    return path


def spending_summary(meter, output):
    """Keep native solver cost separate from metered setup, Loom and judge traffic."""
    gateway = meter.summary()
    path = output / "study/live-attempts.json"
    entries = read(path) if path.exists() else []
    costs = [entry["result"].get("generation_cost_usd") for entry in entries]
    known = [value for value in costs if type(value) in (int, float)]
    protocol = output / "study/protocol.json"
    expected = len(read(protocol)["schedule"]) if protocol.exists() else None
    complete = expected is not None and len(costs) == expected and len(known) == expected
    return {
        "metered_setup_loom_judging": gateway,
        "native_solver": {
            "observed_attempts": len(costs),
            "expected_attempts": expected,
            "known_cost_attempts": len(known),
            "observed_known_cost_usd": sum(known),
            "complete": complete,
        },
        "total_provider_cost_usd": gateway["spent_usd"] + sum(known)
        if complete and gateway["complete"]
        else None,
    }


def preserve_ledger(output):
    """Capture scoped request evidence before a disposable server is removed."""
    from .billing import audit_provider_costs

    protocol, attempts = output / "study/protocol.json", output / "study/live-attempts.json"
    if not protocol.exists() or not attempts.exists():
        return
    try:
        from .runtime import verify

        frozen = read(protocol)
        verify(frozen)
        entries = read(attempts)
        receipt = audit_provider_costs(frozen, entries)
        write(
            output / "ledger-preservation.json",
            {
                "captured": True,
                "known_attempts": sum(
                    row["proof_status"] == "known" for row in receipt["attempts"].values()
                ),
                "cohort_complete": receipt["cohort_complete"],
            },
        )
    except Exception as error:  # noqa: BLE001 — evidence failure must not skip resource cleanup
        write(
            output / "ledger-preservation.json",
            {"captured": False, "error_type": type(error).__name__},
        )
        return
    from .trace_capture import capture_or_report

    for entry in entries:
        capture_or_report(frozen, entry["order"])


def execute(args):
    from . import study

    output, source = args.output.resolve(strict=True), args.source_root.resolve(strict=True)
    launch = read(output / "launch.json")
    if launch["owner"] != args.owner or Path(launch["source_root"]).resolve() != source:
        raise PermissionError("Controller launch ownership or source differs")
    phases = Phases(output, args.owner)
    if args.command == "cleanup":
        runtime = Provisioner(source, output, args.owner)
        receipt = runtime.cleanup()
        write(output / "cleanup-receipt.json", receipt)
        return receipt
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise PermissionError("An API credential is required")
    if launch["budget_usd"] != args.budget_usd:
        raise PermissionError("Provider allowance differs from the launch record")
    if launch["evaluation"] != args.eval:
        raise PermissionError("Repository evaluation differs from the launch record")
    profile = profile_for(source, args.eval)
    proxy = os.environ.get("HTTPS_PROXY")
    if proxy:
        docker_config = Path.home() / ".docker/config.json"
        if docker_config.exists():
            raise PermissionError("The controller must have a fresh Docker configuration")
        docker_config.parent.mkdir(mode=0o700, exist_ok=True)
        docker_config.write_text(json.dumps({"proxies": {"default": {"httpsProxy": proxy}}}))
        docker_config.chmod(0o600)
    descriptor = load_evaluation(source, args.eval)
    meter = Spending(
        output / "spending.jsonl",
        dollars=args.budget_usd,
        model=read(descriptor["paths"]["suite_lock"])["model_id"],
        native_model=native_model(source),
    )
    gateway = runtime = None
    try:
        if meter.blocked:
            raise PermissionError("Unsettled provider requests must be reconciled before resuming")
        gateway_token = secrets.token_urlsafe(32)
        gateway = Gateway(meter, os.environ["ANTHROPIC_API_KEY"], gateway_token)
        port = gateway.start()
        credential = output / "gateway.env"
        credential.write_text("ANTHROPIC_API_KEY=" + gateway_token + "\n")
        credential.chmod(0o600)
        inference = {
            "base_url": f"http://127.0.0.1:{port}",
            "credentials_file": str(credential),
        }
        controller = launch["containers"][-1]["id"]

        def before_paid(phase, operation, details):
            if meter.blocked:
                raise PermissionError("Unsettled supplier billing blocks further setup")
            phases.append(
                "operation_started", "native-setup", operation=operation, operation_phase=phase
            )

        def after_paid(phase, operation, receipt):
            phases.append(
                "operation_completed",
                "native-setup",
                operation=operation,
                operation_phase=phase,
                request_id=receipt.get("request_id"),
            )

        runtime = Provisioner(
            source,
            output,
            args.owner,
            before_paid=before_paid,
            after_paid=after_paid,
            gateway={
                "url": f"http://factory-gateway:{port}",
                "token": gateway_token,
                "controller_container": controller,
            },
        )
        core_python = os.environ["FACTORY_LOOM_PYTHON"]
        meter.set_phase("native-setup")
        phases.run("runtime", runtime.start)
        phases.run(
            "assets", lambda: profile.create_assets(output / "assets", loom_python=core_python)
        )
        phases.run(
            "dependencies",
            lambda: profile.build_dependencies(output / "assets", loom_python=core_python),
        )
        materialized = phases.run(
            "scenarios", lambda: profile.materialize_cases(output / "assets", output / "scenarios")
        )
        compiled = phases.run(
            "compilers",
            lambda: profile.prepare_compiler_artifacts(
                output / "assets",
                materialized,
                output / "compilers",
                loom_python=core_python,
                owner=args.owner,
            ),
        )
        prepared = phases.run(
            "inputs",
            lambda: profile.plan_inputs(
                output / "assets",
                materialized,
                output / "inputs",
            ),
        )
        if launch["pilot_task"] is not None or launch.get("cache_pairs", False):
            from .pilot import select

            prepared = phases.run(
                "pilot-selection" if launch["pilot_task"] is not None else "cache-selection",
                lambda: select(
                    prepared,
                    launch["pilot_task"],
                    output / "pilot-inputs",
                    cache_pairs=launch.get("cache_pairs", False),
                ),
            )
        meter.set_phase("native-setup")
        setup = phases.run(
            "native-setup",
            lambda: runtime.prepare(
                prepared["tasks"],
                prepared["schedule"],
                prepared["memory_histories"],
                compiled,
                loom_schema_profile=launch["loom_schema_profile"],
            ),
        )
        plan = phases.run(
            "plan",
            lambda: protocol_receipt(
                profile.freeze_plan(
                    prepared,
                    setup,
                    output / "plan",
                    inference=inference,
                    budget_usd=args.budget_usd,
                )
            ),
        )
        bootstrap_path = checked_protocol_path(plan)
        staged = phases.run(
            "harbor",
            lambda: protocol_receipt(study.prepare(bootstrap_path, output / "study")),
        )
        for kind in ("base", "reference", "live"):
            meter.set_phase(kind)
            protocol_path = checked_protocol_path(staged)
            phases.run(
                kind,
                lambda kind=kind, protocol_path=protocol_path: asyncio.run(
                    study.run(protocol_path, kind, meter=meter)
                ),
            )
        summary = {
            "owner": args.owner,
            "profile": args.eval,
            "protocol": str(protocol_path),
            "spending": spending_summary(meter, output),
            "report": str(output / "study/reports/REPORT.md"),
        }
        write(output / "evaluation.json", summary)
        return summary
    finally:
        try:
            try:
                preserve_ledger(output)
                write(output / "spending-summary.json", spending_summary(meter, output))
            finally:
                if runtime is not None:
                    write(output / "cleanup-receipt.json", runtime.cleanup())
        finally:
            try:
                if gateway is not None:
                    gateway.close()
            finally:
                meter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "resume", "cleanup"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--eval", required=True)
    parser.add_argument("--budget-usd")
    args = parser.parse_args()

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        print(json.dumps(execute(args), indent=2))
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 — cleanup runs before exposing only the error type
        print(json.dumps({"state": "stopped", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
