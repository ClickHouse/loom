"""Independent full-suite outcomes and regressions against the same frozen base."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from examples.software_factory.evals.flask.historical import junit_statuses


def interpret(xml: Path, exit_code: int, timed_out: bool) -> dict:
    if timed_out:
        return {"valid": False, "status": "timed_out", "tests": None}
    if exit_code not in (0, 1) or not xml.exists():
        return {"valid": False, "status": "runner_error", "tests": None}
    try:
        statuses = junit_statuses(xml)
    except (ValueError, OSError, ET.ParseError):
        return {"valid": False, "status": "invalid_report", "tests": None}
    failures = sum(status == "FAILED" for status in statuses.values())
    valid = all(status in {"PASSED", "FAILED", "SKIPPED"} for status in statuses.values())
    valid = valid and ((exit_code == 0) == (failures == 0))
    return {
        "valid": valid,
        "status": "completed" if valid else "test_runtime_error",
        "tests": statuses if valid else None,
        "failed": failures if valid else None,
        "skipped": sum(status == "SKIPPED" for status in statuses.values()) if valid else None,
    }


def regressions(base: dict | None, candidate: dict | None) -> list[str] | None:
    if (
        not base
        or not candidate
        or base.get("valid") is not True
        or candidate.get("valid") is not True
    ):
        return None
    # A disappearing or newly skipped test also loses a passing base assertion.
    return sorted(
        name
        for name, status in base["tests"].items()
        if status == "PASSED" and candidate["tests"].get(name) != "PASSED"
    )


def evaluate(workspace: Path, destination: Path, run_process) -> dict:
    started = time.monotonic()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="factory-full-suite-") as temporary:
        results = Path(temporary)
        results.chmod(0o777)
        xml = results / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_family=legacy",
            "--junitxml=" + str(xml),
            "tests",
        ]
        exit_code, timed_out = run_process(command, workspace, results)
        result = interpret(xml, exit_code, timed_out)
        for name in ("junit.xml", "stdout", "stderr"):
            if (results / name).exists():
                shutil.copyfile(results / name, destination / name)
    return {
        **result,
        "duration_s": time.monotonic() - started,
        "exit_code": exit_code,
        "scope": "full_frozen_test_suite",
        "agent_feedback": False,
    }
