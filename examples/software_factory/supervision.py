"""Hard deadline for one trusted controller process, retaining its private receipts.

The model never receives this capability. The caller freezes the entrypoint and
reconciles recorded owned containers after the process group has stopped.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .cleanup import remove_owned


def run_bounded(
    argv: list[str], *, cwd: Path, env: dict[str, str], output: Path, timeout_s: float
) -> dict:
    if not argv or not 0 < timeout_s <= 210:
        raise ValueError("Freeze a trusted command and a deadline of at most 210 seconds")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    with (output / "stdout").open("wb") as stdout, (output / "stderr").open("wb") as stderr:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        try:
            while not os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                if time.monotonic() - started >= timeout_s:
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            # WNOWAIT pins the leader PID until the group is stopped, preventing reuse.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            for receipt in output.rglob("factory-container.json"):
                if not json.loads(receipt.read_text()).get("cleaned"):
                    remove_owned(receipt)
    result = {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_s": time.monotonic() - started,
        "output": str(output),
    }
    (output / "supervision.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
