#!/usr/bin/env python3
"""Move ratchet-baseline entries with the files they count.

The baselines are keyed by path (`file` or `file::function`), so a moved file
shows up to a gate as a new key over its baseline of 0, and `--update` refuses
to write while any key has regressed. This tool re-keys the committed entries
of a move in all three baselines, carrying each count unchanged:

    python scripts/gates/baseline_rename.py --rename server/a.py server/pkg/a.py
    python scripts/gates/baseline_rename.py --rename server/old/ server/new/
    python scripts/gates/baseline_rename.py --rename-map moves.csv   # old_path,new_path rows

An OLD ending in `/` moves every path under it. A path with no baseline entry
is fine (most files carry no debt). It refuses, writing nothing, when two
mappings disagree, when a new key already has an entry (the two counts would
merge), or when a baseline's total would change. After a move run both gates:
they pass when the move changed no counted code, and `--update` then only
lowers.

Exit codes: 0 re-keyed (or nothing to do), 2 refused.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.gates.ratchet import (  # noqa: E402
    GateError,
    load_baseline,
    normalise_key,
    write_baseline,
)

BASELINES = (
    "scripts/gates/function_length_baseline.json",
    "scripts/gates/function_length_lines_baseline.json",
    "scripts/gates/mypy_baseline.json",
)


def parse_map(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    """`{old: new}` from (old, new) pairs; a pair repeated verbatim is one move."""
    moves: dict[str, str] = {}
    for raw_old, raw_new in pairs:
        old, new = normalise_key(raw_old.strip()), normalise_key(raw_new.strip())
        if not old or not new:
            raise GateError(f"empty path in move {raw_old!r} -> {raw_new!r}")
        if old == new:
            raise GateError(f"{old!r} moves to itself")
        if old.endswith("/") != new.endswith("/"):
            raise GateError(f"{old!r} -> {new!r}: a directory (trailing /) moves to a directory")
        if moves.get(old, new) != new:
            raise GateError(f"{old!r} moves to both {moves[old]!r} and {new!r}")
        moves[old] = new
    return moves


def read_map(path: Path) -> list[tuple[str, str]]:
    """The (old_path, new_path) rows of a CSV file; a header row naming the
    columns and blank lines are skipped."""
    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    except OSError as e:
        raise GateError(f"cannot read {path}: {e}") from e
    pairs: list[tuple[str, str]] = []
    for n, row in enumerate(rows, 1):
        cells = [c.strip() for c in row]
        if not any(cells) or (n == 1 and cells == ["old_path", "new_path"]):
            continue
        if len(cells) != 2:
            raise GateError(f"{path}:{n}: expected old_path,new_path, got {row!r}")
        pairs.append((cells[0], cells[1]))
    return pairs


def _moved(path: str, moves: Mapping[str, str]) -> str:
    if path in moves:
        return moves[path]
    for old, new in moves.items():
        if old.endswith("/") and path.startswith(old):
            return new + path[len(old) :]
    return path


def rekey(counts: Mapping[str, int], moves: Mapping[str, str]) -> tuple[dict[str, int], list[str]]:
    """`counts` with every key whose file part moved re-keyed, and the moves
    made as `old -> new` lines. Raises GateError when a re-keyed entry would
    land on a key that already has one."""
    out: dict[str, int] = {}
    source: dict[str, str] = {}
    changed: list[str] = []
    for key, count in sorted(counts.items()):
        path, sep, member = key.partition("::")
        new_key = _moved(path, moves) + sep + member
        if new_key in out:
            raise GateError(f"{key!r} and {source[new_key]!r} would both become {new_key!r}")
        out[new_key], source[new_key] = count, key
        if new_key != key:
            changed.append(f"{key} -> {new_key} ({count})")
    if sum(out.values()) != sum(counts.values()):  # re-keying never changes a count
        raise GateError("the baseline total changed while re-keying")
    return out, changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--rename", nargs=2, action="append", default=[], metavar=("OLD", "NEW"))
    ap.add_argument("--rename-map", type=Path, help="CSV of old_path,new_path rows")
    ap.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: this checkout)"
    )
    ap.add_argument("--dry-run", action="store_true", help="print the re-keying, write nothing")
    args = ap.parse_args(argv)
    try:
        pairs = [(old, new) for old, new in args.rename]
        if args.rename_map:
            pairs += read_map(args.rename_map)
        moves = parse_map(pairs)
        if not moves:
            ap.error("give --rename OLD NEW or --rename-map FILE")
        planned = []
        for rel in BASELINES:
            path = args.root / rel
            new, changed = rekey(load_baseline(path), moves)
            planned.append((path, new, changed))
    except GateError as e:
        print(f"baseline-rename: refused, nothing written: {e}", file=sys.stderr)
        return 2
    for path, new, changed in planned:
        noun = "entry" if len(changed) == 1 else "entries"
        print(f"{path.relative_to(args.root).as_posix()}: {len(changed)} {noun} re-keyed")
        for line in changed:
            print(f"  {line}")
        if changed and not args.dry_run:
            write_baseline(path, new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
