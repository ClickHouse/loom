"""Prepare historical agent inputs from pinned independent control receipts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .behavioral import BehavioralGrader
from .historical import FrozenSource, HistoricalGrader, controller_hashes
from .qualify import load_inputs, public_task


def prepare_historical(
    task_id: str,
    output: Path,
    *,
    private_config: Path,
    qualification: Path,
    qualification_sha256: str,
    namespace: str,
    symbol: str,
):
    """Return a source-only Task and private grader, never a reference candidate.

    An external eligibility check must authorize and freeze the namespace. This
    function grants no access, runs no tests and constructs no provider client.
    """
    if not namespace or not symbol:
        raise ValueError("Freeze the exact index namespace and named intake symbol")
    raw = qualification.read_bytes()
    if hashlib.sha256(raw).hexdigest() != qualification_sha256:
        raise PermissionError("Independent control receipt differs from the protocol")
    controls = json.loads(raw)
    public, config, private, visible, selected = load_inputs(task_id, private_config)
    expected = {
        "kind": "focused-historical-controls",
        "task_id": task_id,
        "base_commit": public["base_commit"],
        "dependency_image": public["dependency_image"],
        "config_sha256": public["grader_config_sha256"],
        "private_task_sha256": config["task_sha256"],
        "visible_selectors": visible,
        "hidden_selectors": selected,
        "qualified": True,
    }
    if any(controls.get(k) != v for k, v in expected.items()):
        raise PermissionError("Historical control identity or selectors are unqualified")
    # Other controller modules may evolve; the grading implementation must match.
    current = controller_hashes()
    for name in ("historical.py", "workspace.py", "model.py", "cleanup.py"):
        if controls.get("controller_sha256", {}).get(name) != current[name]:
            raise PermissionError("Historical grading implementation requires new controls")
    fail, passing = json.loads(private["FAIL_TO_PASS"]), json.loads(private["PASS_TO_PASS"])
    for name in ("visible-base", "base", "reference"):
        row = controls.get("controls", {}).get(name, {})
        statuses = row.get("selected", {})
        expected_statuses = {
            node: "FAILED" if name == "base" and node in fail else "PASSED"
            for node in (visible if name == "visible-base" else selected)
        }
        if (
            row.get("valid") is not True
            or row.get("passed") is not (name != "base")
            or statuses != expected_statuses
            or not all(statuses[node] == "PASSED" for node in passing if node in statuses)
        ):
            raise PermissionError("Independent base/reference controls are invalid")
    source = FrozenSource(Path(config["source_git"]), public["base_commit"])
    task = source.task(public, namespace, symbol)
    grader = HistoricalGrader(
        source,
        public["dependency_image"],
        visible,
        selected,
        private["test_patch"],
        output / "grading",
    )
    if task.source_sha256 != controls["source_sha256"] or any(
        row["grader_sha256"] != grader.sha256 for row in controls["controls"].values()
    ):
        raise PermissionError("Prepared source or grader differs from independent controls")
    return task, grader


def prepare_behavioral(
    task_id: str,
    output: Path,
    *,
    private_config: Path,
    qualification: Path,
    qualification_sha256: str,
    namespace: str,
    symbol: str,
):
    """Prepare only exact-base source after fresh authored behavioral controls.

    The frozen config supplies a mirror path. The private task file, reference
    patch and official hidden tests are not read during attempt preparation.
    """
    if not namespace or not symbol:
        raise ValueError("Freeze the exact index namespace and named intake symbol")
    raw = qualification.read_bytes()
    if hashlib.sha256(raw).hexdigest() != qualification_sha256:
        raise PermissionError("Behavioral control receipt differs from the protocol")
    controls, public = json.loads(raw), public_task(task_id)
    config_raw = private_config.read_bytes()
    if hashlib.sha256(config_raw).hexdigest() != public["grader_config_sha256"]:
        raise PermissionError("Independent grader config differs from the audited receipt")
    source = FrozenSource(Path(json.loads(config_raw)["source_git"]), public["base_commit"])
    task = source.task(public, namespace, symbol)
    expected = {
        "kind": "authored-host-behavioral-controls",
        "task_id": task_id,
        "base_commit": task.base_commit,
        "source_sha256": task.source_sha256,
        "dependency_image": public["dependency_image"],
        "config_sha256": public["grader_config_sha256"],
        "qualified": True,
        "coverage": "authored-public-behavior-not-official-pytest",
    }
    if any(controls.get(k) != v for k, v in expected.items()):
        raise PermissionError("Behavioral controls do not match this task")
    grader = BehavioralGrader(
        source,
        task_id,
        public["dependency_image"],
        controls["seed"],
        output / "grading",
    )
    if controls.get("grader_sha256") != grader.sha256:
        raise PermissionError("Behavioral grading implementation requires new controls")
    verify_behavioral_controls(controls, grader)
    return task, grader


def verify_behavioral_controls(controls: dict, grader: BehavioralGrader) -> None:
    visible = {p.key for p in grader.probes if p.visible}
    for name in ("visible-base", "base", "reference"):
        row = controls.get("controls", {}).get(name, {})
        statuses = row.get("selected", {})
        expected = visible if name == "visible-base" else {p.key for p in grader.probes}
        if (
            row.get("valid") is not True
            or row.get("result_authority") != "host"
            or row.get("passed") is not (name != "base")
            or row.get("grader_sha256") != grader.sha256
            or set(statuses) != expected
            or any(type(v) is not bool for v in statuses.values())
            or any(not statuses[k] for k in visible)
            or all(statuses.values()) is not (name != "base")
        ):
            raise PermissionError("Behavioral base/reference controls are invalid")
