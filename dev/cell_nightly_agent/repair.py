#!/usr/bin/env python3
"""Run only inside the monitor's private tmux server and systemd cgroup."""

import json
import subprocess
import sys
import time
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent


def main():
    owned = json.loads((TASK_DIR / "last-tmux-ownership.json").read_text())
    if Path("/proc/self/cgroup").read_text().strip() != owned["cgroup"]:
        raise RuntimeError("Repair runner is outside the bounded service cgroup")
    if "--probe-wait" in sys.argv:
        import os

        (TASK_DIR / "probe-child.json").write_text(
            json.dumps(dict(pid=os.getpid(), cgroup=Path("/proc/self/cgroup").read_text()))
        )
        time.sleep(30)
        return
    if "--probe" in sys.argv:
        result = dict(status="synthetic_probe_passed", disposition="no_model_or_api_calls")
    else:
        config = json.loads((TASK_DIR / "settings.json").read_text())
        argv = [
            "codex",
            "--no-daemon",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "danger-full-access",
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
            "exec",
            "--ephemeral",
            "--output-schema",
            str(TASK_DIR / "result-schema.json"),
            "-C",
            config["checkout"],
            "-o",
            str(TASK_DIR / "last-agent-status.json"),
            "-",
        ]
        (TASK_DIR / "last-agent-status.json").unlink(missing_ok=True)
        with (
            (TASK_DIR / "current-task.md").open() as prompt,
            (TASK_DIR / "last-agent.log").open("wb") as log,
        ):
            process = subprocess.Popen(
                argv, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            saved = 0
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                if saved < 1024 * 1024:
                    selected = chunk[: 1024 * 1024 - saved]
                    log.write(selected)
                    saved += len(selected)
            code = process.wait()
        report = TASK_DIR / "last-agent-status.json"
        try:
            status = json.loads(report.read_text()) if report.stat().st_size <= 65536 else {}
        except (OSError, ValueError):
            status = {}
        result = dict(
            status="completed" if code == 0 else "failed",
            exit_code=code,
            disposition=status.get("disposition", "unfinished"),
            report=status,
        )
    temporary = TASK_DIR / "repair-result.tmp"
    temporary.write_text(json.dumps(result) + "\n")
    temporary.replace(TASK_DIR / "repair-result.json")


if __name__ == "__main__":
    main()
