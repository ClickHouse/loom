#!/usr/bin/env python3
"""mypy ratchet gate, per file.

The codebase carries pre-existing strict-mypy errors (`[tool.mypy] strict`).
They are frozen per file in `scripts/gates/mypy_baseline.json` (`{path: count}`).
This gate runs mypy and fails when ANY file has more errors than its baseline
entry, so a touched file cannot gain errors and debt cannot move between files.

When a file's count falls, the gate says so; run `--update` to lower the
baseline (it only ever lowers). A file with 0 errors is dropped from the file.

Environment divergence: mypy reports `Library stubs not installed for "x"`
when the running interpreter lacks a `types-*` package that pyproject's dev
extras install in CI. Those lines are not type errors in the code, so they are
excluded from the per-file counts and printed separately; the gate then reads
the same here and in CI. A stub package that pyproject does NOT list is
counted like any other error.

A key is a file and a count is its number of `error:` lines, so the gate does
not depend on message text or error codes, which change between mypy
releases. The parsed total must equal mypy's own `Found N errors` summary:
a line the parser cannot read fails the gate rather than going uncounted.

Usage:
    python scripts/gates/mypy_gate.py            # gate (exit 1 on any regression)
    python scripts/gates/mypy_gate.py --update   # lower the baseline to today's counts
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.gates.ratchet import GateError, Ratchet, Report, normalise_key  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("mypy_baseline.json")
TARGETS = ["server", "sdk/python/loom", "extractor/loom_extractor"]

_ERROR_RE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\s][^:]*\.pyi?):(?P<line>\d+):(?:\d+:)? error: (?P<msg>.*)$"
)
_FOUND_RE = re.compile(r"^Found (?P<n>\d+) errors? in \d+ files?", re.MULTILINE)
_SUCCESS_RE = re.compile(r"^Success: no issues found", re.MULTILINE)
_STUB_RE = re.compile(r'^Library stubs not installed for "(?P<module>[^"]+)"')
_HINT_RE = re.compile(r'pip install (?P<pkg>[\w\-\.]+)"?')


def _listed_stub_packages(pyproject: Path) -> set[str]:
    """`types-*` packages pyproject declares (any extras), lower-cased."""
    if not pyproject.exists():
        return set()
    return {m.lower() for m in re.findall(r'"(types-[\w\-\.]+)', pyproject.read_text("utf-8"))}


def parse(output: str, *, listed_stubs: set[str]) -> tuple[Counter[str], list[str]]:
    """Per-file error counts and the environment-only lines excluded from them.

    A `Library stubs not installed` error is environment-only when the stub
    package mypy's hint names (or `types-<module>`) is in pyproject: CI
    installs it, the local venv may not.
    """
    counts: Counter[str] = Counter()
    env_only: list[str] = []
    lines = output.splitlines()
    # mypy prints the `pip install types-X` hint once per module, after the
    # first occurrence; collect module → package over the whole output first.
    hinted: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = _ERROR_RE.match(line)
        stub = _STUB_RE.match(m.group("msg")) if m else None
        if stub and i + 1 < len(lines):
            hint = _HINT_RE.search(lines[i + 1])
            if hint:
                hinted[stub.group("module")] = hint.group("pkg").lower()
    for line in lines:
        m = _ERROR_RE.match(line)
        if not m:
            continue
        stub = _STUB_RE.match(m.group("msg"))
        if stub:
            module = stub.group("module")
            if hinted.get(module, f"types-{module}".lower()) in listed_stubs:
                env_only.append(line)
                continue
        counts[normalise_key(m.group("path"))] += 1
    return counts, env_only


def reported_total(output: str) -> int:
    """mypy's own error total from its summary line; GateError when absent."""
    found = _FOUND_RE.search(output)
    if found:
        return int(found.group("n"))
    if _SUCCESS_RE.search(output):
        return 0
    raise GateError("mypy printed no `Found N errors` or `Success` summary")


def run_mypy() -> str:
    """mypy's combined output. Exit 0 is clean and 1 is type errors; anything
    else (2 is a crash, a config error or a blocking error) cannot be gated."""
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *TARGETS], capture_output=True, text=True, cwd=ROOT
    )
    out = proc.stdout + proc.stderr
    if proc.returncode not in (0, 1):
        print(out)
        raise GateError(f"mypy exited {proc.returncode}")
    return out


class MypyRatchet(Ratchet):
    name = "mypy-gate"
    unit = "error(s)"
    command = "python scripts/gates/mypy_gate.py"

    def __init__(self, baseline_path: Path) -> None:
        super().__init__(baseline_path)
        self._output = ""
        self._env_only: list[str] = []

    def measure(self) -> Mapping[str, int]:
        out = run_mypy()
        try:
            total = reported_total(out)
        except GateError:
            print(out)
            raise
        self._output = out
        counts, self._env_only = parse(
            out, listed_stubs=_listed_stub_packages(ROOT / "pyproject.toml")
        )
        parsed = sum(counts.values()) + len(self._env_only)
        if parsed != total:
            print(out)
            raise GateError(f"parsed {parsed} error line(s) but mypy reported {total}")
        return counts

    def headline(self, current: Mapping[str, int], baseline: Mapping[str, int]) -> list[str]:
        lines = [
            f"{self.name}: {sum(current.values())} error(s) in {len(current)} file(s) "
            f"(baseline {sum(baseline.values())} in {len(baseline)} file(s))"
        ]
        if self._env_only:
            lines.append(
                f"{self.name}: {len(self._env_only)} environment-only line(s) excluded — "
                "library stubs pyproject lists are not installed here (CI installs them):"
            )
            lines += [f"    {line}" for line in self._env_only]
        return lines

    def evidence(self, report: Report) -> list[str]:
        env_only = set(self._env_only)
        return [
            f"    {line}"
            for path in report.regressions
            for line in self._output.splitlines()
            if line.startswith(path + ":") and " error: " in line and line not in env_only
        ]

    def failure(self, report: Report) -> str:
        return (
            f"{len(report.regressions)} file(s) above their baseline. "
            "Fix the new errors; the baseline only ratchets down."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--update", action="store_true", help="lower the baseline to today's counts")
    ap.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    args = ap.parse_args(argv)
    return MypyRatchet(args.baseline).run(update=args.update)


if __name__ == "__main__":
    raise SystemExit(main())
