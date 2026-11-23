"""Emit bounded HTTP contract status without failure payloads."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_TEST_FILE = "tests/contracts/test_http_schema.py"
_SEEN: set[tuple[int | None, str]] = set()


def pytest_runtest_logreport(report: Any) -> None:
    if not report.failed:
        return
    location = report.location
    if not isinstance(location, tuple) or len(location) < 2 or location[0] != _TEST_FILE:
        return
    line = location[1]
    if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line < 1_000_000:
        line = None
    phase = report.when if report.when in {"setup", "call", "teardown"} else "unknown"
    key = (line, phase)
    if key in _SEEN or len(_SEEN) >= 10:
        return
    _SEEN.add(key)
    position = "" if line is None else f",line={line + 1}"
    print(f"::error file={_TEST_FILE}{position}::HTTP contract failed during {phase}.", flush=True)


def pytest_collectreport(report: Any) -> None:
    if report.failed and (None, "collection") not in _SEEN and len(_SEEN) < 10:
        _SEEN.add((None, "collection"))
        print("::error::HTTP contract collection failed.", flush=True)


def verify_report(path: Path) -> bool:
    if not path.is_file():
        print("::error::HTTP contract report is missing.")
        return False
    try:
        with path.open("rb") as source:
            payload = source.read(1024 * 1024 + 1)
    except OSError:
        print("::error::HTTP contract report cannot be read.")
        return False
    if len(payload) > 1024 * 1024:
        print("::error::HTTP contract report exceeds the size limit.")
        return False
    try:
        suites = ET.fromstring(payload).findall(".//testsuite")
        counts = {}
        for field in ("tests", "skipped", "failures", "errors"):
            values = [suite.get(field, "0") for suite in suites]
            if any(
                not value.isascii() or not value.isdecimal() or len(value) > 5 for value in values
            ):
                raise ValueError
            counts[field] = sum(int(value) for value in values)
            if counts[field] > 10_000:
                raise ValueError
    except (ET.ParseError, ValueError):
        print("::error::HTTP contract report has invalid XML or counts.")
        return False
    print("HTTP contract counts: " + json.dumps(counts, sort_keys=True))
    if counts != {"tests": 10, "skipped": 0, "failures": 0, "errors": 0}:
        print("::error::HTTP contract requires ten tests without skips, failures or errors.")
        return False
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
        output.write(
            "HTTP contract: 10 tests, 0 skipped, 0 failures/errors. Three properties use "
            "max_examples=24; shrinking shares the subprocess time limit. Six fixed invalid-body "
            "cases and a Unicode-header regression also ran. Backing stores are synthetic; "
            "database isolation is not claimed.\n"
        )
    return True


if __name__ == "__main__":
    raise SystemExit(
        0 if verify_report(Path(os.environ["RUNNER_TEMP"]) / "http-contract.xml") else 1
    )
