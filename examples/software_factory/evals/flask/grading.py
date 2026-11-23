"""Run frozen pytest selectors on an allowlisted candidate in a Harbor environment."""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from examples.software_factory.evals.flask.historical import (
    junit_statuses,
    ordinary,
    selected_statuses,
    validate_fixtures,
)
from examples.software_factory.factory.model import digest


def validate_candidate(files: dict, base: dict, expected_source: str) -> dict:
    allowed = {k: v for k, v in base.items() if k.startswith("src/flask/")}
    if digest(allowed) != expected_source:
        raise PermissionError("Verifier source base differs from the frozen task")
    if not isinstance(files, dict) or any(
        not ordinary(path) or not path.startswith("src/flask/") for path in files
    ):
        raise PermissionError("Candidate source escapes the implementation root")
    if any(type(v) is not str or len(v.encode()) > 1024 * 1024 for v in files.values()):
        raise ValueError("Candidate file exceeds the source bound")
    return files


def child_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)


def stage_candidate(candidate_path: Path) -> tuple[dict, dict, Path]:
    spec = json.loads(Path("/factory/spec.json").read_text())
    base = json.loads(Path("/factory/base.json").read_text())
    if candidate_path.is_symlink() or not candidate_path.is_dir():
        raise ValueError("Native source artifact must be a directory")
    files, total = {}, 0
    for path in sorted(candidate_path.rglob("*")):
        if path.is_symlink():
            raise PermissionError("Candidate source contains a symbolic link")
        if not path.is_file():
            continue
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Candidate file exceeds the source bound")
        content = path.read_bytes()
        total += len(content)
        if total > 20 * 1024 * 1024:
            raise ValueError("Candidate exceeds the source bound")
        files["src/flask/" + path.relative_to(candidate_path).as_posix()] = content.decode("utf-8")
    files = validate_candidate(files, base, spec["source_sha256"])
    candidate = {"state": digest(files), "files": files}
    destination = Path("/logs/verifier")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "candidate.json").write_text(json.dumps(candidate) + "\n")
    fixtures = validate_fixtures(
        json.loads(Path("/factory/fixtures.json").read_text()), base, spec["fixtures_sha256"]
    )
    workspace = Path("/verified-workspace")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(mode=0o755)
    remaining = {
        name: content for name, content in base.items() if not name.startswith("src/flask/")
    }
    for name, content in {**remaining, **files, **fixtures}.items():
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
                    "PYTHONPATH": str(workspace / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_ADDOPTS": "",
                },
                stdout=stdout,
                stderr=stderr,
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
        Path("/logs/verifier/grade.json").write_text(json.dumps(result) + "\n")
        Path("/logs/verifier/reward.txt").write_text(str(int(result["passed"])))
        from examples.software_factory.evals.flask.full_suite import evaluate

        full = evaluate(workspace, Path("/logs/verifier/full-suite"), run_regression_process)
        Path("/logs/verifier/full-suite.json").write_text(json.dumps(full) + "\n")
    return result


def run_regression_process(
    command: list[str], workspace: Path, results: Path, *, deadline_s: int | None = None
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
                "PYTHONPATH": str(workspace / "src"),
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
                if deadline_s is not None and time.monotonic() - started >= deadline_s:
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.returncode, timed_out


if __name__ == "__main__":
    print(json.dumps(grade(sys.argv[1], Path(sys.argv[2]))))
