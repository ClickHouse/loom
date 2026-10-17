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
# The dump with the bi-temporal journal on (its migration applied): the objects only
# it has are read from here.
TEMPORAL_SCHEMA = SCHEMA.with_name("schema_temporal.sql")


@cache
def schema_text() -> str:
    return SCHEMA.read_text()


@cache
def temporal_schema_text() -> str:
    return TEMPORAL_SCHEMA.read_text()


def ddl(name: str, *, temporal: bool = False) -> str:
    """The CREATE statement of `loom.<name>`, through its closing `;`: from the
    default dump, or the journal's for an object only it has (`temporal` reads the
    journal's dump first)."""
    for text in (
        (temporal_schema_text(), schema_text())
        if temporal
        else (schema_text(), temporal_schema_text())
    ):
        if f"-- loom.{name}\n" in text:
            start = text.index(f"-- loom.{name}\n")
            return text[start : text.index(";\n", start) + 1]
    raise ValueError(f"loom.{name} is in neither schema dump")


_OBJECT = re.compile(r"^CREATE (TABLE|MATERIALIZED VIEW) IF NOT EXISTS loom\.(\w+)", re.M)
_COLUMN = re.compile(r"^    `(\w+)` (.+?),?$")


@cache
def _kinds() -> dict[str, str]:
    both = schema_text() + temporal_schema_text()
    return {name: kind for kind, name in _OBJECT.findall(both)}


def objects() -> set[str]:
    """Every `loom.*` table and materialized view either dump declares."""
    return set(_kinds())


def tables() -> set[str]:
    return {n for n, kind in _kinds().items() if kind == "TABLE"}


def views() -> set[str]:
    return {n for n, kind in _kinds().items() if kind != "TABLE"}


def columns(name: str, *, materialized: bool = True, temporal: bool = False) -> dict[str, str]:
    """`{column: type and modifiers}` of `loom.<name>` in declaration order.
    `materialized=False` leaves out MATERIALIZED columns, which no writer
    carries; `temporal` reads the table as the journal's migration leaves it."""
    block = re.split(r"\n(?:ENGINE =|AS )", ddl(name, temporal=temporal), maxsplit=1)[0]
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
