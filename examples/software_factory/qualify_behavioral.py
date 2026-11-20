"""Run fresh authored host-oracle controls, without models or index operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .behavioral import BehavioralGrader
from .historical import FrozenSource, git
from .qualify import load_inputs
from .workspace import Checkout


def qualify(task_id: str, config_path: Path, output: Path, seed: str) -> dict:
    public, config, private, _, _ = load_inputs(task_id, config_path)
    source = FrozenSource(Path(config["source_git"]), public["base_commit"])
    task = source.task(public, "unqualified-no-index", "controls-only")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    controls = {}
    for control in ("visible-base", "base", "reference"):
        checkout = Checkout(output / (control + "-candidate"), task)
        if control == "reference":
            # Only the private control sees this patch; never return it as a task.
            git(
                checkout.root, "apply", "--include=src/flask/*", "-", data=private["patch"].encode()
            )
        grader = BehavioralGrader(
            source, task_id, public["dependency_image"], seed, output / control
        )
        hidden = control != "visible-base"
        result = grader.run(checkout, hidden=hidden)
        host = json.loads(
            (grader.output / ("hidden" if hidden else "visible-1") / "host-grade.json").read_text()
        )
        visible_keys = {p.key for p in grader.probes if p.visible}
        valid = result["passed"] is (control != "base") and all(
            passed for key, passed in host["selected"].items() if key in visible_keys
        )
        controls[control] = {**host, "valid": valid}
    receipt = {
        "kind": "authored-host-behavioral-controls",
        "task_id": task_id,
        "base_commit": public["base_commit"],
        "source_sha256": task.source_sha256,
        "dependency_image": public["dependency_image"],
        "config_sha256": public["grader_config_sha256"],
        "seed": seed,
        "grader_sha256": grader.sha256,
        "controls": controls,
        "qualified": all(row["valid"] for row in controls.values()),
        "coverage": "authored-public-behavior-not-official-pytest",
        "provider_requests": 0,
        "index_requests": 0,
        "model_resolutions": None,
    }
    (output / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--private-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args()
    result = qualify(args.task, args.private_config, args.output, args.seed)
    print(
        json.dumps(
            {k: result[k] for k in ("task_id", "qualified", "coverage", "provider_requests")}
        )
    )
    if not result["qualified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
