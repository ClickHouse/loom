"""Split a test run into parts that run on separate machines.

`LOOM_TEST_SHARD=k/n` runs the k-th of n parts of the test files under the paths
pytest was given; `tests/conftest.py` calls `ignored` from `pytest_ignore_collect`,
so a part imports and collects only its own files. A part holds whole files, so a
module-scoped fixture is built once, in one part.

Files are dealt heaviest first to the lightest part. A file's weight is the seconds
`tests/shard_weights.json` records for it; a file it does not list weighs its test
functions times the seconds a recorded test function takes on average (one second
each when nothing is recorded). The weights only balance the parts: every process
given the same files and weights computes the same split, and each file lands in
exactly one part. `python -m tests._shards <junit xml>...` rewrites the weights
from a run's JUnit reports.
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS = ROOT / "tests" / "shard_weights.json"
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test", re.MULTILINE)
_SKIP_DIRS = {"__pycache__", "node_modules"}


def parse(spec: str) -> tuple[int, int]:
    """`"k/n"` as `(k, n)`, 1 <= k <= n; anything else is a ValueError."""
    index, sep, count = spec.partition("/")
    if not sep or not index.isdecimal() or not count.isdecimal():
        raise ValueError(f"LOOM_TEST_SHARD={spec!r} is not k/n")
    k, n = int(index), int(count)
    if not 1 <= k <= n:
        raise ValueError(f"LOOM_TEST_SHARD={spec!r}: k must be between 1 and n")
    return k, n


def assign(weights: Mapping[str, float], parts: int) -> dict[str, int]:
    """Each file's part, 1-based: heaviest first, to the lightest part so far
    (ties to the lower part, files of equal weight in name order)."""
    load = [0.0] * parts
    out: dict[str, int] = {}
    for name in sorted(weights, key=lambda f: (-weights[f], f)):
        part = load.index(min(load))
        load[part] += weights[name]
        out[name] = part + 1
    return out


def weigh(files: dict[str, str], recorded: dict[str, float]) -> dict[str, float]:
    """Each file's weight: its recorded seconds, else its test functions (counted in
    `files`, path to source) times the recorded seconds per test function."""
    tests = {name: max(1, len(_TEST_DEF.findall(src))) for name, src in files.items()}
    known = [name for name in files if name in recorded]
    per_test = sum(recorded[n] for n in known) / sum(tests[n] for n in known) if known else 1.0
    return {name: recorded.get(name, tests[name] * per_test) for name in files}


def test_files(paths: list[Path]) -> list[Path]:
    """The test modules under `paths`, as pytest's default `test_*.py` and
    `*_test.py` patterns name them."""
    out: set[Path] = set()
    for path in paths:
        if path.is_file():
            out.add(path.resolve())
            continue
        for found in path.rglob("*.py"):
            if _SKIP_DIRS.intersection(found.parts) or any(
                p.startswith(".") for p in found.relative_to(path).parts
            ):
                continue
            if found.name.startswith("test_") or found.name.endswith("_test.py"):
                out.add(found.resolve())
    return sorted(out)


class Split:
    """The files of the requested part, worked out once per session."""

    def __init__(self, k: int, n: int, args: list[Path]) -> None:
        files = test_files(args)
        sources = {_name(f): f.read_text(encoding="utf-8", errors="replace") for f in files}
        recorded = json.loads(WEIGHTS.read_text()) if WEIGHTS.exists() else {}
        part = assign(weigh(sources, recorded), n)
        self.walked = set(part)
        self.kept = {name for name, p in part.items() if p == k}
        self.first = k == 1

    def ignores(self, path: Path) -> bool:
        if path.suffix != ".py" or not (
            path.name.startswith("test_") or path.name.endswith("_test.py")
        ):
            return False
        name = _name(path.resolve())
        if name not in self.walked:
            # A test file the walk did not find runs in part 1, so it still runs once.
            return not self.first
        return name not in self.kept


_split: Split | None = None


def ignored(path: Path, config: pytest.Config) -> bool | None:
    """True for a test file outside the requested part; None (no opinion) otherwise."""
    global _split
    spec = os.environ.get("LOOM_TEST_SHARD", "")
    if not spec:
        return None
    if _split is None:
        try:
            k, n = parse(spec)
        except ValueError as e:
            raise pytest.UsageError(str(e)) from e
        args = [Path(a.split("::", 1)[0]).resolve() for a in config.args] or [ROOT / "tests"]
        _split = Split(k, n, args)
    return True if _split.ignores(path) else None


def _name(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def record(reports: list[Path]) -> dict[str, float]:
    """Seconds per test file, summed over the test cases of JUnit `reports`."""
    seconds: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            name = _module_file(case.get("classname", ""))
            if name:
                seconds[name] += float(case.get("time") or 0.0)
    return {name: round(s, 2) for name, s in sorted(seconds.items())}


def _module_file(classname: str) -> str | None:
    """`tests.unit.test_x` or `tests.unit.test_x.TestY` as `tests/unit/test_x.py`."""
    parts = classname.split(".")
    while parts:
        candidate = "/".join(parts) + ".py"
        if (ROOT / candidate).is_file():
            return candidate
        parts.pop()
    return None


if __name__ == "__main__":
    WEIGHTS.write_text(json.dumps(record([Path(p) for p in sys.argv[1:]]), indent=2) + "\n")
