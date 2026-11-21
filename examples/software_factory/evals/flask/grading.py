"""Run frozen pytest selectors on an allowlisted candidate in a Harbor environment."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from examples.software_factory.evals.flask.historical import (
    fixture_path,
    junit_statuses,
    selected_statuses,
)
from examples.software_factory.factory.model import digest

REGRESSION_SOURCE_BYTES = 16000
REGRESSION_OUTPUT_CHARS = 4000


def validate_regression_source(source: str) -> str:
    if type(source) is not str or not source.strip():
        raise ValueError("Regression source must be nonempty Python text")
    if len(source.encode("utf-8")) > REGRESSION_SOURCE_BYTES:
        raise ValueError("Regression source exceeds the 16000-byte bound")
    try:
        ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ValueError("Regression source must be valid Python: " + str(error)[:500]) from error
    return source


def validate_candidate(candidate: dict, base: dict, expected_source: str) -> dict:
    allowed = {k: v for k, v in base.items() if k.startswith("src/flask/")}
    if digest(allowed) != expected_source or candidate.get("source_sha256") != expected_source:
        raise PermissionError("Candidate and verifier source bases differ")
    files = candidate["files"]
    if not isinstance(files, dict) or set(files) != set(allowed):
        raise PermissionError("Candidate may change only the frozen implementation files")
    if any(type(v) is not str or len(v.encode()) > 1024 * 1024 for v in files.values()):
        raise ValueError("Candidate file exceeds the source bound")
    if candidate.get("state") != digest(files):
        raise PermissionError("Candidate source fingerprint differs")
    return files


def child_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)


def validate_fixtures(fixtures: dict, base: dict, expected: str) -> dict[str, bytes]:
    if digest(fixtures) != expected:
        raise PermissionError("Evaluator fixtures differ from the frozen source")
    restored, total = {}, 0
    for name, encoded in fixtures.items():
        if not fixture_path(name) or name in base or name.startswith("src/flask/"):
            raise PermissionError("Evaluator fixture overlaps source or escapes its workspace")
        content = base64.b64decode(encoded, validate=True)
        total += len(content)
        if len(content) > 1024 * 1024 or total > 20 * 1024 * 1024:
            raise ValueError("Evaluator fixture exceeds the source bound")
        restored[name] = content
    return restored


def stage_candidate(candidate_path: Path) -> tuple[dict, dict, Path]:
    spec = json.loads(Path("/factory/spec.json").read_text())
    base = json.loads(Path("/factory/base.json").read_text())
    if candidate_path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Candidate exceeds the source bound")
    candidate = json.loads(candidate_path.read_text())
    files = validate_candidate(candidate, base, spec["source_sha256"])
    fixtures = validate_fixtures(
        json.loads(Path("/factory/fixtures.json").read_text()), base, spec["fixtures_sha256"]
    )
    workspace = Path("/workspace")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(mode=0o755)
    for name, content in {**base, **files, **fixtures}.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o444)
    return spec, candidate, workspace


def grade(kind: str, candidate_path: Path) -> dict:
    started = time.monotonic()
    spec, candidate, workspace = stage_candidate(candidate_path)
    selectors = spec[kind + "_selectors"]
    with tempfile.TemporaryDirectory(prefix="factory-pytest-") as directory:
        results = Path(directory)
        results.chmod(0o777)
        xml = results / "junit.xml"
        with (results / "stdout").open("wb") as stdout, (results / "stderr").open("wb") as stderr:
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "junit_family=legacy",
                    "--junitxml=" + str(xml),
                    *selectors,
                ],
                cwd=workspace,
                env={
                    "PATH": os.defpath,
                    "HOME": directory,
                    "PYTHONPATH": "/workspace/src",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_ADDOPTS": "",
                },
                stdout=stdout,
                stderr=stderr,
                timeout=30,
                preexec_fn=child_limits,
                check=False,
            )
        statuses = selected_statuses(junit_statuses(xml), selectors)
        if run.returncode not in (0, 1) or any(
            v not in {"PASSED", "FAILED"} for v in statuses.values()
        ):
            raise RuntimeError("Pytest collection or test runtime failed")
        if kind == "hidden":
            target = Path("/logs/verifier")
            target.mkdir(parents=True, exist_ok=True)
            for name in ("junit.xml", "stdout", "stderr"):
                shutil.copyfile(results / name, target / name)
    result = {
        "passed": run.returncode == 0 and all(v == "PASSED" for v in statuses.values()),
        "exit_code": run.returncode,
        "transport_exit_code": 0,
        "duration_s": time.monotonic() - started,
        "image": spec["dependency_image"],
        "state": candidate["state"],
        "grader_sha256": spec["grader_sha256"],
    }
    if kind == "visible":
        result["selected"] = statuses
    else:
        completed = (
            candidate.get("agent_completed") is True
            and candidate.get("visible_passed") is True
            and candidate.get("verified_state") == candidate["state"]
        )
        Path("/logs/verifier/grade.json").write_text(json.dumps(result) + "\n")
        Path("/logs/verifier/reward.txt").write_text(str(int(result["passed"] and completed)))
        from examples.software_factory.evals.flask.full_suite import evaluate

        full = evaluate(workspace, Path("/logs/verifier/full-suite"), run_regression_process)
        Path("/logs/verifier/full-suite.json").write_text(json.dumps(full) + "\n")
    return result


def run_regression_process(
    command: list[str], workspace: Path, results: Path, *, deadline_s: int = 30
) -> tuple[int, bool]:
    """Run only inside the Linux task sandbox; reap the entire test process group."""
    started = time.monotonic()
    with (results / "stdout").open("wb") as stdout, (results / "stderr").open("wb") as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env={
                "PATH": os.defpath,
                "HOME": str(results),
                "PYTHONPATH": "/workspace/src",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_ADDOPTS": "",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            stdout=stdout,
            stderr=stderr,
            preexec_fn=child_limits,
            start_new_session=True,
        )
        timed_out = False
        try:
            # WNOWAIT retains ownership of the leader PID until the group is stopped.
            while not os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                if time.monotonic() - started >= deadline_s:
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.returncode, timed_out


def regression_status(xml: Path, exit_code: int, timed_out: bool) -> dict:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    status = "timed_out" if timed_out else "error"
    if not timed_out and xml.exists() and xml.stat().st_size <= 1024 * 1024:
        try:
            root = ET.parse(xml).getroot()
            cases = list(root.iter("testcase"))
            counts = {
                "tests": len(cases),
                "failures": sum(c.find("failure") is not None for c in cases),
                "errors": sum(c.find("error") is not None for c in cases),
                "skipped": sum(c.find("skipped") is not None for c in cases),
            }
            if (
                exit_code == 0
                and cases
                and not any(counts[k] for k in ("failures", "errors", "skipped"))
            ):
                status = "passed"
            elif exit_code == 1 and counts["failures"]:
                status = "failed"
            elif not cases:
                status = "no_tests"
        except (ET.ParseError, ValueError):
            pass  # Malformed authored-test output is failure evidence, never a passing grade.
    return {"passed": status == "passed", "status": status, **counts}


def bounded_output(path: Path, limit: int) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    # Output files are bounded by the child file-size limit; tails retain pytest's summary.
    text = path.read_text(errors="replace")
    return text[-limit:], len(text) > limit


def regression(candidate_path: Path, authored_path: Path) -> dict:
    started = time.monotonic()
    if authored_path.stat().st_size > REGRESSION_SOURCE_BYTES:
        raise ValueError("Regression source exceeds the 16000-byte bound")
    source = validate_regression_source(authored_path.read_text())
    _, candidate, workspace = stage_candidate(candidate_path)
    with tempfile.TemporaryDirectory(prefix="factory-regression-") as directory:
        scratch = Path(directory)
        scratch.chmod(0o755)
        test_file = scratch / "test_regression.py"
        test_file.write_text(source)
        test_file.chmod(0o444)
        results = scratch / "results"
        results.mkdir(mode=0o777)
        results.chmod(0o777)
        xml = results / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--confcutdir=" + str(scratch),
            "--rootdir=" + str(scratch),
            "--import-mode=importlib",
            "--junitxml=" + str(xml),
            str(test_file),
        ]
        exit_code, timed_out = run_regression_process(command, workspace, results)
        status = regression_status(xml, exit_code, timed_out)
        stdout, stdout_truncated = bounded_output(results / "stdout", 3000)
        stderr, stderr_truncated = bounded_output(results / "stderr", 1000)
        output = (stdout + "\n" + stderr).strip()
    return {
        **status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": output[:REGRESSION_OUTPUT_CHARS],
        "output_truncated": (
            stdout_truncated or stderr_truncated or len(output) > REGRESSION_OUTPUT_CHARS
        ),
        "state": candidate["state"],
        "authored_test_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "duration_s": time.monotonic() - started,
        "scope": "agent_authored_regression",
        "task_completion_verified": False,
    }


if __name__ == "__main__":
    if sys.argv[1] == "regression":
        result = regression(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        result = grade(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result))
