"""Exercise the cohort CLI with authored replay drivers and no external I/O."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.runner import load_driver
from examples.software_factory.tests.test_controller import protocol

DRIVER = """
from examples.software_factory.tests.controller_worker import prepare, ScriptedProvider

def verify_eligibility(protocol):
    if protocol.get("authored_fixture") is not True:
        raise PermissionError("fixture eligibility missing")

def provider_factory(output, audit):
    return ScriptedProvider(output)
"""


class RunnerContracts(unittest.TestCase):
    def test_cli_supervises_all_cold_attempts_and_can_resume_completed_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.py"
            driver.write_text(DRIVER)
            plan = protocol()
            plan.update(
                authored_fixture=True, driver_sha256=hashlib.sha256(driver.read_bytes()).hexdigest()
            )
            manifest = root / "protocol.json"
            manifest.write_text(json.dumps(plan))
            command = [
                sys.executable,
                "-m",
                "examples.software_factory.runner",
                "--protocol",
                str(manifest),
                "--driver",
                str(driver),
                "--output",
                str(root / "cohort"),
            ]
            for expected in (2, 0):
                run = subprocess.run(
                    command,
                    cwd=Path(__file__).resolve().parents[3],
                    env={"PATH": os.defpath},
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                rows = [json.loads(line) for line in run.stdout.splitlines()]
                self.assertEqual(len(rows), expected)
                for row in rows:
                    self.assertTrue(row["result"]["resolved"])
                    self.assertEqual(row["result"]["provider"], "scripted-replay")
                    self.assertEqual(row["result"]["loom_logical_requests"], 0)
                    self.assertIsNone(row["result"]["total_cost_usd"])

    def test_changed_driver_refuses_before_executing_its_module_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver, sentinel = root / "driver.py", root / "executed"
            driver.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")
            with self.assertRaisesRegex(PermissionError, "differs"):
                load_driver(driver, {"driver_sha256": "0" * 64})
            self.assertFalse(sentinel.exists())

    def test_missing_host_contract_refuses_without_constructing_a_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "driver.py"
            driver.write_text("def verify_eligibility(protocol): pass\n")
            with self.assertRaisesRegex(ValueError, "prepare"):
                load_driver(
                    driver, {"driver_sha256": hashlib.sha256(driver.read_bytes()).hexdigest()}
                )


if __name__ == "__main__":
    unittest.main()
