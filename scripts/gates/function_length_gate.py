#!/usr/bin/env python3
"""Function-length ratchet gate: two ratchets over `server` and `sdk/python`.

Per file, by statements: ruff's PLR0915 (too-many-statements, threshold
MAX_STATEMENTS) is the measure; `scripts/gates/function_length_baseline.json`
freezes how many functions each file has over it (`{path: count}`). A new
oversized function, or a split that leaves two oversized halves, fails.

Per function, by lines: `scripts/gates/function_length_lines_baseline.json`
freezes `{"file::function": lines}` for every function over INVENTORY_LINES
lines. A listed function may not grow past its entry and an unlisted one may
not cross the line, so an oversized function cannot keep growing under a
green per-file count.

Neither ratchet covers the other: a new 60-statement, 90-line function moves
only the per-file count; a listed function growing by one line moves only the
per-function one.

`--update` lowers both baselines (never raises them). `--inventory` prints
every function over the statement threshold with its statement count and
line span, plus the functions over INVENTORY_LINES lines.

Usage:
    python scripts/gates/function_length_gate.py              # gate
    python scripts/gates/function_length_gate.py --update     # lower the baselines
    python scripts/gates/function_length_gate.py --inventory  # the table, no gate
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.gates.ratchet import GateError, Ratchet, Report  # noqa: E402

# Both relative to `--root`.
BASELINE = "scripts/gates/function_length_baseline.json"
LINES_BASELINE = "scripts/gates/function_length_lines_baseline.json"
TARGETS = ["server", "sdk/python"]
MAX_STATEMENTS = 50
INVENTORY_LINES = 200
COMMAND = "python scripts/gates/function_length_gate.py"


@dataclass(frozen=True)
class FunctionSpan:
    name: str  # qualified: `Class.method`, `outer.inner`
    first: int
    last: int

    @property
    def lines(self) -> int:
        return self.last - self.first + 1


@dataclass(frozen=True)
class Oversized:
    """One PLR0915 finding, resolved to its function."""

    file: str
    line: int
    function: str
    statements: int
    lines: int


@dataclass(frozen=True, order=True)
class LongFunction:
    lines: int
    file: str
    function: str
    first: int

    @property
    def key(self) -> str:
        return f"{self.file}::{self.function}"


def _check_targets(root: Path, targets: list[str]) -> None:
    missing = [t for t in targets if not (root / t).is_dir()]
    if missing:
        raise GateError(f"target director{'ies' if len(missing) > 1 else 'y'} missing: {missing}")


def ruff_findings(root: Path, targets: list[str]) -> list[dict[str, Any]]:
    """PLR0915 findings as ruff's JSON: `filename`, `location.row`, `message`."""
    _check_targets(root, targets)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "PLR0915",
            "--config",
            f"lint.pylint.max-statements={MAX_STATEMENTS}",
            "--output-format",
            "json",
            "--exit-zero",
            *targets,
        ],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if proc.returncode != 0:
        raise GateError(f"ruff failed ({proc.returncode}): {proc.stderr.strip()}")
    # `--exit-zero` with JSON output prints `[]` when clean; no output at all
    # means ruff did not run the check.
    try:
        findings = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise GateError(f"ruff printed no JSON findings list ({e}): {proc.stdout[:200]!r}") from e
    if not isinstance(findings, list):
        raise GateError(f"ruff printed {type(findings).__name__}, not a findings list")
    return findings


def _relative(root: Path, filename: str) -> str:
    p = Path(filename)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def counts_from_findings(root: Path, findings: list[dict[str, Any]]) -> Counter[str]:
    return Counter(_relative(root, str(f["filename"])) for f in findings)


def _functions(path: Path) -> list[FunctionSpan]:
    """Every def in the file, nested ones included; GateError when unparsable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        raise GateError(f"cannot parse {path}: {e}") from e
    out: list[FunctionSpan] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                out.append(FunctionSpan(name, child.lineno, child.end_lineno or child.lineno))
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def inventory(root: Path, findings: list[dict[str, Any]]) -> list[Oversized]:
    """One row per PLR0915 finding, most statements first."""
    rows: list[Oversized] = []
    by_file: dict[str, list[FunctionSpan]] = {}
    for f in findings:
        rel = _relative(root, str(f["filename"]))
        row = int(f["location"]["row"])
        tokens = str(f["message"]).replace("(", " ").replace(">", " ").split()
        digits = [int(tok) for tok in tokens if tok.isdigit()]
        funcs = by_file.setdefault(rel, _functions(root / rel))
        # ruff reports the `def` line (or the decorator line); take the def
        # whose span starts at or just after the reported row.
        span = next((fn for fn in funcs if row <= fn.first <= row + 8), FunctionSpan("?", row, row))
        rows.append(Oversized(rel, row, span.name, digits[0] if digits else 0, span.lines))
    rows.sort(key=lambda r: (-r.statements, r.file))
    return rows


def long_functions(root: Path, targets: list[str], min_lines: int) -> list[LongFunction]:
    """Every function over `min_lines` lines, longest first."""
    _check_targets(root, targets)
    rows = [
        LongFunction(span.lines, _relative(root, str(path)), span.name, span.first)
        for target in targets
        for path in sorted((root / target).rglob("*.py"))
        for span in _functions(path)
        if span.lines > min_lines
    ]
    return sorted(rows, reverse=True)


class StatementRatchet(Ratchet):
    """Per file: how many functions are over MAX_STATEMENTS."""

    name = "function-length-gate"
    unit = "oversized function(s)"
    command = COMMAND

    def __init__(self, root: Path, findings: list[dict[str, Any]], baseline_path: Path) -> None:
        super().__init__(baseline_path)
        self.root, self.findings = root, findings

    def measure(self) -> Mapping[str, int]:
        return counts_from_findings(self.root, self.findings)

    def headline(self, current: Mapping[str, int], baseline: Mapping[str, int]) -> list[str]:
        return [
            f"{self.name}: {sum(current.values())} function(s) over {MAX_STATEMENTS} "
            f"statements in {len(current)} file(s) (baseline {sum(baseline.values())} in "
            f"{len(baseline)} file(s))"
        ]

    def evidence(self, report: Report) -> list[str]:
        return [
            f"    {r.file}:{r.line}: {r.function} has {r.statements} statements, {r.lines} lines"
            for r in inventory(self.root, self.findings)
            if r.file in report.regressions
        ]

    def failure(self, report: Report) -> str:
        return (
            f"{len(report.regressions)} file(s) gained an oversized function "
            f"(> {MAX_STATEMENTS} statements). Split it; the baseline only ratchets down."
        )


class LineRatchet(Ratchet):
    """Per function: its length in lines, for every function over `min_lines`."""

    name = "function-length-gate"
    unit = "lines"
    subject = "function(s)"
    label = "per-function baseline"
    command = COMMAND

    def __init__(self, root: Path, baseline_path: Path, min_lines: int = INVENTORY_LINES) -> None:
        super().__init__(baseline_path)
        self.root, self.min_lines = root, min_lines
        self._long: list[LongFunction] = []

    def measure(self) -> Mapping[str, int]:
        self._long = long_functions(self.root, TARGETS, self.min_lines)
        return {fn.key: fn.lines for fn in self._long}

    def evidence(self, report: Report) -> list[str]:
        return [
            f"    {fn.file}:{fn.first}: {fn.function} is {fn.lines} lines"
            for fn in self._long
            if fn.key in report.regressions
        ]

    def headline(self, current: Mapping[str, int], baseline: Mapping[str, int]) -> list[str]:
        return [
            f"{self.name}: {len(current)} function(s) over {self.min_lines} lines "
            f"(baseline {len(baseline)})"
        ]

    def failure(self, report: Report) -> str:
        return (
            f"{len(report.regressions)} function(s) over {self.min_lines} lines grew past "
            "their entry or crossed the line. Split it; the per-function baseline only "
            "ratchets down."
        )


def print_inventory(root: Path, findings: list[dict[str, Any]]) -> None:
    rows = inventory(root, findings)
    files = len({r.file for r in rows})
    print(f"{len(rows)} function(s) over {MAX_STATEMENTS} statements in {files} file(s)")
    print(f"{'statements':>10}  {'lines':>6}  function")
    for r in rows:
        print(f"{r.statements:>10}  {r.lines:>6}  {r.file}::{r.function}")
    long = long_functions(root, TARGETS, INVENTORY_LINES)
    print(f"\n{len(long)} function(s) over {INVENTORY_LINES} lines")
    for fn in long:
        print(f"{fn.lines:>6}  {fn.key}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--update", action="store_true", help="lower the baselines to today's counts")
    ap.add_argument("--inventory", action="store_true", help="print the per-function table")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--baseline", type=Path, help=f"per-file baseline (default <root>/{BASELINE})")
    ap.add_argument(
        "--lines-baseline",
        type=Path,
        help=f"per-function baseline (default <root>/{LINES_BASELINE})",
    )
    args = ap.parse_args(argv)
    root: Path = args.root
    try:
        findings = ruff_findings(root, TARGETS)
        if args.inventory:
            print_inventory(root, findings)
            return 0
    except GateError as e:
        print(f"function-length-gate: {e} — treating as FAILURE")
        return 2
    by_statements = StatementRatchet(root, findings, args.baseline or root / BASELINE)
    by_lines = LineRatchet(root, args.lines_baseline or root / LINES_BASELINE)
    # Both run, so one failure does not hide the other's report.
    return max(by_statements.run(update=args.update), by_lines.run(update=args.update))


if __name__ == "__main__":
    raise SystemExit(main())
