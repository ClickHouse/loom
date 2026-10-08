"""Branch-coverage floors for the modules where an untested branch is a
security, data-integrity or lifecycle risk.

The floors are not a repository-wide target. They are per-module ratchets
set just under the measured branch coverage, so a change that adds
untested branches to an authorization, write-path, erasure, migration or
supervisor module fails, while the rest of the tree is judged by its tests'
behaviour. Floors live in coverage_floors.toml with the reason each module
is critical.

    pytest tests/unit --cov=server --cov-branch --cov-report=json:coverage.json
    python -m tests.inventory.coverage_floors coverage.json

It exits 1 when a module is under its floor and prints a hint when a
module has risen well above it (raise the floor).
"""

from __future__ import annotations

import argparse
import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLOORS = Path(__file__).resolve().parent / "coverage_floors.toml"
# A module this far above its floor should have the floor raised.
RAISE_MARGIN = 3.0


@dataclass(frozen=True)
class Floor:
    prefix: str
    branch: float
    reason: str


@dataclass(frozen=True)
class Result:
    floor: Floor
    covered: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 100.0

    @property
    def below(self) -> bool:
        return self.total == 0 or self.percent < self.floor.branch

    @property
    def can_raise(self) -> bool:
        return self.percent - self.floor.branch >= RAISE_MARGIN


def load_floors(path: Path = FLOORS) -> list[Floor]:
    data = tomllib.loads(path.read_text())
    out = []
    for prefix, entry in data.get("floors", {}).items():
        if set(entry) != {"branch", "reason"} or not str(entry["reason"]).strip():
            raise ValueError(f"{path.name}: {prefix} needs exactly `branch` and `reason`")
        out.append(Floor(prefix, float(entry["branch"]), entry["reason"]))
    return out


def measure(report: Mapping[str, Any], floors: Sequence[Floor]) -> list[Result]:
    """Branch totals per floor from a coverage.py JSON report. A floor whose
    prefix matches no measured file counts as below, since a renamed module
    must not silently drop out of the gate."""
    if not report.get("meta", {}).get("branch_coverage"):
        raise ValueError("the report has no branch data; run pytest with --cov-branch")
    files = report["files"]
    results = []
    for floor in floors:
        matched = [v["summary"] for k, v in files.items() if k.startswith(floor.prefix)]
        results.append(
            Result(
                floor,
                covered=sum(s["covered_branches"] for s in matched),
                total=sum(s["num_branches"] for s in matched),
            )
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", type=Path, help="coverage.py JSON report (--cov-report=json)")
    args = ap.parse_args(argv)
    results = measure(json.loads(args.report.read_text()), load_floors())
    failed = False
    for r in results:
        mark = "BELOW" if r.below else ("raise" if r.can_raise else "ok")
        print(f"{mark:5s} {r.percent:6.1f}% (floor {r.floor.branch:5.1f}) {r.floor.prefix}")
        failed |= r.below
    if failed:
        print("A critical module lost branch coverage: test the new branches, or explain in")
        print("the PR why the floor in tests/inventory/coverage_floors.toml moves.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
