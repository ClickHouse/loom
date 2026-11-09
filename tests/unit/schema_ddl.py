"""Read one loom object's DDL from the canonical schema dump.

Tests that pin a table's shape read it here rather than from a migration
file: `schemas/schema.sql` is what a fresh replay produces (CI's
schema-check keeps it so), whichever migration introduced the object.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "schema.sql"


@cache
def schema_text() -> str:
    return SCHEMA.read_text()


def ddl(name: str) -> str:
    """The CREATE statement of `loom.<name>` (or of a qualified
    `loom_telemetry.<name>`), through its closing `;`."""
    text = schema_text()
    start = text.index(f"-- {name if '.' in name else 'loom.' + name}\n")
    return text[start : text.index(";\n", start) + 1]


_OBJECT = re.compile(r"^CREATE (TABLE|MATERIALIZED VIEW|VIEW) IF NOT EXISTS loom\.(\w+)", re.M)
_COLUMN = re.compile(r"^    `(\w+)` (.+?),?$")


@cache
def _kinds() -> dict[str, str]:
    return {name: kind for kind, name in _OBJECT.findall(schema_text())}


def objects() -> set[str]:
    """Every `loom.*` table and view the dump declares."""
    return set(_kinds())


def tables() -> set[str]:
    return {n for n, kind in _kinds().items() if kind == "TABLE"}


def views() -> set[str]:
    """The materialized views."""
    return {n for n, kind in _kinds().items() if kind == "MATERIALIZED VIEW"}


def columns(name: str, *, materialized: bool = True) -> dict[str, str]:
    """`{column: type and modifiers}` of `loom.<name>` in declaration order.
    `materialized=False` leaves out MATERIALIZED columns, which no writer
    carries."""
    block = re.split(r"\n(?:ENGINE =|AS )", ddl(name), maxsplit=1)[0]
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = _COLUMN.match(line)
        if m and (materialized or " MATERIALIZED " not in f" {m.group(2)} "):
            out[m.group(1)] = m.group(2)
    return out


def column_names(name: str, *, materialized: bool = True) -> list[str]:
    """Column names of `loom.<name>` in declaration order."""
    return list(columns(name, materialized=materialized))


def engine(name: str) -> str:
    """The engine name of table `loom.<name>` (`ReplacingMergeTree`, ...)."""
    m = re.search(r"^ENGINE = (\w+)", ddl(name), re.M)
    assert m, f"loom.{name} has no ENGINE clause"
    return m.group(1)


def ttl(name: str) -> str | None:
    """The table-level TTL expression of `loom.<name>`, or None."""
    m = re.search(r"^TTL (.+)$", ddl(name), re.M)
    return m.group(1) if m else None


def view_select(name: str) -> str:
    """The SELECT a materialized view `loom.<name>` runs, without the `;`."""
    return ddl(name).split("\nAS ", 1)[1].rstrip(";")
