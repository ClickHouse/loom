"""Run a selected synthetic contribution check without a Loom deployment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_TESTS = {
    "functional": "tests/unit/test_provenance.py",
    "security": "tests/unit/test_authz_target_validated.py",
    "performance": "tests/unit/test_llm_request_timeout.py",
    "stress": "tests/unit/test_code_budget_tiers.py",
    "usability": "tests/unit/test_cli_surface_snapshot.py",
}


def check(track: str, directory: Path) -> dict[str, object]:
    if track == "quality":
        target = "server/memory/domain/provenance.py"
        commands = [
            [sys.executable, "-m", "ruff", "check", "--no-cache", target],
            [sys.executable, "-m", "ruff", "format", "--check", "--no-cache", target],
        ]
        results = [
            subprocess.run(command, check=False, timeout=120).returncode for command in commands
        ]
        return {
            "track": track,
            "checks": len(results),
            "passed": all(code == 0 for code in results),
        }
    with tempfile.TemporaryDirectory(dir=directory) as run_directory:
        report = Path(run_directory) / "report.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            _TESTS[track],
            "-o",
            "cache_dir=/tmp/loom-pytest-cache",
            f"--junitxml={report}",
        ]
        result = subprocess.run(command, check=False, timeout=120)
        suites = ET.parse(report).getroot().findall(".//testsuite") if report.is_file() else []
        total = sum(int(suite.get("tests", "0")) for suite in suites)
        skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
        passed = result.returncode == 0 and total > skipped
        return {"track": track, "tests": total, "skipped": skipped, "passed": passed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choices = [*_TESTS, "quality", "all"]
    parser.add_argument("track", choices=choices, default="functional", nargs="?")
    parser.add_argument(
        "--list", action="store_true", help="Print selected inputs without running checks"
    )
    args = parser.parse_args()
    if args.list:
        print(
            json.dumps({"tests": _TESTS, "quality": "server/memory/domain/provenance.py"}, indent=2)
        )
        return
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    directory = Path("/tmp/loom-contributor-lab")
    directory.mkdir(exist_ok=True)
    tracks = choices[:-1] if args.track == "all" else [args.track]
    results = []
    for track in tracks:
        results.append(check(track, directory))
        (directory / "report.json").write_text(json.dumps(results, indent=2) + "\n")
        if not results[-1]["passed"]:
            raise SystemExit(1)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
