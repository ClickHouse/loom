"""Native monkey patches are isolated from pytest's import order."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("second", ["recall", "remember", "knowledge", "credentials"])
def test_native_invocations_overlap_with_scoped_credentials(second):
    case = Path(__file__).with_name("native_concurrency_case.py")
    result = subprocess.run(
        [sys.executable, "-I", str(case), second],
        env={"PATH": os.defpath, "HOME": os.environ["HOME"], "LOOM_ENV_FILE": ""},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    evidence = result.stdout + result.stderr
    assert result.returncode == 0, evidence
    assert "SERIAL_NATIVE_IMPORT_PASS" in result.stdout
    assert "NATIVE_CONCURRENCY_PASS " + second in result.stdout
    for diagnostic in (
        "Traceback (most recent call last):",
        "Exception in thread",
        "RuntimeWarning:",
        "was never awaited",
        "Task was destroyed",
    ):
        assert diagnostic not in evidence, evidence
