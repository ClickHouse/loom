"""Validate frozen inputs; refuse candidate-written qualification reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import NoReturn


def public_task(task_id: str) -> dict:
    candidates = json.loads(Path(__file__).with_name("historical-candidates.json").read_text())
    return next(t for t in candidates["tasks"] if t["task_id"] == task_id)


def load_inputs(task_id: str, config_path: Path) -> tuple:
    """Validate the same frozen private inputs for controls and cold workers."""
    public = public_task(task_id)
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
    return public, config, private, visible, selected


def qualify(task_id: str, config_path: Path, output: Path) -> NoReturn:
    raise PermissionError("Historical reports cannot provide candidate-independent grading")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.task, args.config, args.output)


if __name__ == "__main__":
    main()
