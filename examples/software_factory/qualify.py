"""Requalify one frozen historical case without models or index access."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .historical import (
    FrozenSource,
    HistoricalGrader,
    controller_hashes,
    git,
    junit_statuses,
    selected_statuses,
)
from .model import digest
from .workspace import Checkout


def qualify(task_id: str, config_path: Path, output: Path) -> dict:
    controller = controller_hashes()
    candidates = json.loads(Path(__file__).with_name("historical-candidates.json").read_text())
    public = next(t for t in candidates["tasks"] if t["task_id"] == task_id)
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != public["grader_config_sha256"]:
        raise PermissionError("Independent grader config differs from the audited receipt")
    config = json.loads(config_bytes)
    private_path = Path(config["task_file"]).resolve(strict=True)
    if not private_path.is_relative_to(config_path.resolve().parent):
        raise PermissionError("Private data leaves its frozen controller directory")
    private_bytes = private_path.read_bytes()
    if hashlib.sha256(private_bytes).hexdigest() != config["task_sha256"]:
        raise PermissionError("Independent private task changed")
    private = json.loads(private_bytes)
    if private["base_commit"] != public["base_commit"]:
        raise PermissionError("Independent and public source bases differ")
    fail, passing = json.loads(private["FAIL_TO_PASS"]), json.loads(private["PASS_TO_PASS"])
    selected = list(dict.fromkeys(fail + passing))
    if (
        not fail
        or not passing
        or selected != config["selected_tests"]
        or selected != public["objective_selectors"]
    ):
        raise PermissionError("Frozen objective selectors disagree")
    visible = [
        node
        for node, status in public["prior_controls"]["visible-base"]["selected_tests"].items()
        if status == "PASSED" and node in passing
    ]
    if not visible:
        raise PermissionError("Visible selectors must be public-base pass-to-pass tests")
    source = FrozenSource(Path(config["source_git"]), public["base_commit"])
    task = source.task(public, "unqualified-no-index", "qualification-only")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    controls = {}
    for control in ("visible-base", "base", "reference"):
        candidate = Checkout(output / (control + "-candidate"), task)
        if control == "reference":
            git(
                candidate.root,
                "apply",
                "--include=src/flask/*",
                "-",
                data=private["patch"].encode(),
            )
        grader = HistoricalGrader(
            source,
            public["dependency_image"],
            visible,
            selected,
            private["test_patch"],
            output / control,
        )
        result = grader.run(candidate, hidden=control != "visible-base")
        xml = (
            grader.output
            / ("visible-1" if control == "visible-base" else "hidden")
            / "test/results/junit.xml"
        )
        statuses = selected_statuses(
            junit_statuses(xml), visible if control == "visible-base" else selected
        )
        valid = all(
            statuses[n] == "PASSED" for n in (visible if control == "visible-base" else passing)
        )
        if control == "base":
            valid = (
                valid and result["exit_code"] == 1 and all(statuses[n] == "FAILED" for n in fail)
            )
        if control in {"visible-base", "reference"}:
            valid = valid and result["passed"]
        controls[control] = {
            "valid": valid,
            "passed": result["passed"],
            "selected": statuses,
            "grader_sha256": grader.sha256,
            "state": candidate.state(),
        }
        (output / (control + ".json")).write_text(json.dumps(controls[control], indent=2) + "\n")
    if controller_hashes() != controller:
        raise PermissionError("Controller source changed during qualification")
    receipt = {
        "kind": "focused-historical-controls",
        "task_id": task_id,
        "base_commit": source.commit,
        "dependency_image": public["dependency_image"],
        "source_sha256": task.source_sha256,
        "config_sha256": public["grader_config_sha256"],
        "private_task_sha256": config["task_sha256"],
        "controls": controls,
        "visible_selectors": visible,
        "hidden_selectors": selected,
        "qualified": all(row["valid"] for row in controls.values()),
        "provider_requests": 0,
        "loom_requests": 0,
        "paid_benchmark": False,
        "controller_sha256": controller,
    }
    receipt["receipt_sha256"] = digest(receipt)
    (output / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.task, args.config, args.output)
    print(
        json.dumps(
            {
                "task_id": args.task,
                "qualified": result["qualified"],
                "receipt_sha256": result["receipt_sha256"],
            }
        )
    )
    raise SystemExit(0 if result["qualified"] else 1)


if __name__ == "__main__":
    main()
