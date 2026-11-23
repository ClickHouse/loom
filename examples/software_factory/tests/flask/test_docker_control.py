"""Daemon delays must not shorten or extend the candidate's execution limit."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.software_factory.evals.flask.historical import DockerTests
from examples.software_factory.harbor.cleanup import remove_owned


class DockerControlContracts(unittest.TestCase):
    def test_delayed_daemon_operations_preserve_the_test_deadline(self):
        for delayed in ("create", "state", "ownership", "rm"):
            with self.subTest(delayed=delayed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                output = root / "grade"
                calls = []

                def run(argv, *, delayed=delayed, calls=calls, output=output, **kwargs):
                    phase = argv[1]
                    if phase == "inspect":
                        phase = "state" if argv[3] == "{{json .State}}" else "ownership"
                    calls.append((phase, kwargs["timeout"]))
                    if phase == delayed and kwargs["timeout"] < 12:
                        raise subprocess.TimeoutExpired(argv, 12)
                    if phase == "start":
                        (output / "results/junit.xml").write_text(
                            '<testsuite errors="0"><testcase file="tests/a.py" '
                            'classname="tests.a" name="test_x"/></testsuite>'
                        )
                    owner = json.loads((output / "factory-container.json").read_text())["owner"]
                    state = {"ExitCode": 0, "Running": False, "OOMKilled": False, "Error": ""}
                    return SimpleNamespace(
                        returncode=0,
                        stdout=owner if phase == "ownership" else json.dumps(state),
                        stderr="",
                    )

                with patch("subprocess.run", side_effect=run):
                    result = DockerTests(
                        "sha256:" + "a" * 64, ["tests/a.py::test_x"], output, timeout_s=7
                    ).run(workspace)
                self.assertTrue(result["passed"])
                self.assertEqual(dict(calls)["start"], 7)
                self.assertTrue(
                    json.loads((output / "factory-container.json").read_text())["cleaned"]
                )

    def test_unresponsive_daemon_keeps_ownership_receipt_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "factory-container.json"
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "factory-container-ownership-v1",
                        "owner": "a" * 32,
                        "name": "loom-factory-" + "a" * 32,
                        "cleaned": False,
                    }
                )
            )
            with patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 60)
            ) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    remove_owned(receipt)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(json.loads(receipt.read_text())["cleaned"])
