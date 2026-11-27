"""Exercise the historical test boundary with authored attacks and no model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from examples.software_factory.evals.flask.historical import DockerTests, controller_hashes

FIXTURE = """import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


def test_source_is_readonly():
    with pytest.raises(OSError):
        Path("tests/test_isolation.py").write_text("candidate grades itself")


def test_results_are_separate():
    Path("/results/probe").write_text("allowed")
    assert not Path("/workspace/probe").exists()


def test_no_history_or_controller_files():
    assert not Path("/workspace/.git").exists()
    assert not Path("/controller-only-canary").exists()
    assert not Path("/workspace/controller-only-canary").exists()
    assert not Path("/var/run/docker.sock").exists()
    with pytest.raises((FileNotFoundError, PermissionError)):
        with Path("/root/.aws/credentials").open("rb"):
            pass
    assert not Path("/home/ubuntu").exists()


def test_no_credential_environment():
    assert not any(name.startswith(("AWS_", "LOOM_")) for name in os.environ)


def test_no_metadata_egress():
    with socket.socket() as stream:
        stream.settimeout(1)
        assert stream.connect_ex(("169.254.169.254", 80)) != 0


def test_subprocess_has_same_boundary():
    probe = ("from pathlib import Path; "
             "assert not Path('/home/ubuntu').exists(); "
             "assert not Path('/var/run/docker.sock').exists()")
    result = subprocess.run([sys.executable, "-c", probe], timeout=2)
    assert result.returncode == 0
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    controller = controller_hashes()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (args.output / "controller-only-canary").write_text("not agent-visible")
    workspace = args.output / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests/test_isolation.py").write_text(FIXTURE)
    selectors = [
        "tests/test_isolation.py::" + name
        for name in (
            "test_source_is_readonly",
            "test_results_are_separate",
            "test_no_history_or_controller_files",
            "test_no_credential_environment",
            "test_no_metadata_egress",
            "test_subprocess_has_same_boundary",
        )
    ]
    result = DockerTests(args.image, selectors, args.output / "test").run(workspace)
    if controller_hashes() != controller:
        raise PermissionError("Controller source changed during qualification")
    result["controller_sha256"] = controller
    result.update(kind="authored-isolation-probes", paid_benchmark=False, provider_requests=0)
    (args.output / "qualification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
