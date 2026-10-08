"""A ReplacingMergeTree keeps the row with the highest version under FINAL, so
every writer of a table must stamp versions in one unit. A millisecond version
written after a nanosecond one is a million times smaller and never wins: the
later edit is lost without an error.

The unit is nanoseconds. A `UInt64` version column defaults to a nanosecond
clock or has no default (every writer sends it), and no server writer builds a
version from `timestamp() * 1000`.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.schema_ddl import columns, ddl, engine, tables

ROOT = Path(__file__).resolve().parents[2]

_ENGINE_ARGS = re.compile(r"^ENGINE = ReplacingMergeTree\((\w+)", re.M)
_MS_VERSION = re.compile(r"\.timestamp\(\)\s*\*\s*1_?000\)")


def _unit(expr: str) -> str:
    if "Milli" in expr:
        return "ms"
    if "Nano" in expr or "* 1000000000" in expr:
        return "ns"
    return "unknown"


def _integer_versions() -> dict[str, str | None]:
    """`{table: default expression or None}` for every ReplacingMergeTree whose
    version column is an integer (DateTime versions are seconds by type)."""
    out: dict[str, str | None] = {}
    for name in tables():
        if engine(name) != "ReplacingMergeTree":
            continue
        m = _ENGINE_ARGS.search(ddl(name))
        if m is None:
            continue
        spec = columns(name)[m.group(1)]
        if not spec.startswith("UInt64"):
            continue
        d = re.search(r"\b(?:DEFAULT|MATERIALIZED) (.+?)(?: CODEC\(.*)?$", spec)
        out[name] = d.group(1) if d else None
    return out


def test_integer_versions_default_to_nanoseconds_or_to_nothing() -> None:
    units = {n: _unit(e) for n, e in _integer_versions().items() if e is not None}
    assert len(units) > 40
    assert not {n: u for n, u in units.items() if u == "unknown"}
    assert not sorted(n for n, u in units.items() if u == "ms")


def test_no_writer_builds_a_millisecond_version() -> None:
    found: dict[str, int] = {}
    for path in sorted((ROOT / "server").rglob("*.py")):
        hits = [line for line in path.read_text().splitlines() if _MS_VERSION.search(line)]
        if hits:
            found[path.relative_to(ROOT).as_posix()] = len(hits)
    assert not found, found


def test_the_unit_classifier() -> None:
    assert _unit("toUnixTimestamp64Milli(now64(3))") == "ms"
    assert _unit("toUInt64(toUnixTimestamp64Nano(now64(9)))") == "ns"
    assert _unit("toUnixTimestamp(updated_at) * 1000000000") == "ns"
    assert _MS_VERSION.search("version = int(now.timestamp() * 1000)")
    assert not _MS_VERSION.search("ms = int((time.monotonic() - t0) * 1000)")
