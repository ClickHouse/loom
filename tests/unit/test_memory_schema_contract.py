"""Schema contract tests for load-bearing ClickHouse row shapes."""

from __future__ import annotations

from pathlib import Path

from server.insights.app.ops import REQUIRED_SCHEMA_COLUMNS
from server.memory.store import MEMORY_PERSISTED_COLS


def _columns_from_schema(table: str, *, materialized: bool = True) -> set[str]:
    """Extract one loom table's column names from the canonical schema dump.

    `materialized=False` leaves out the columns the table computes itself
    (`value_preview`, `value_len`) — no writer
    carries those, so they are not persisted columns.
    """

    schema = Path("schemas/schema.sql").read_text()
    marker = f"-- loom.{table}\n"
    start = schema.index(marker)
    end = schema.index("\nENGINE =", start)
    block = schema[start:end]
    columns: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("`"):
            continue
        if not materialized and " MATERIALIZED " in stripped:
            continue
        columns.add(stripped.split("`", 2)[1])
    return columns


def test_memory_persisted_cols_match_schema_dump() -> None:
    """RMT version writers must mirror every persisted loom.memory column."""

    schema_columns = _columns_from_schema("memory", materialized=False)
    mirrored_columns = set(MEMORY_PERSISTED_COLS)

    assert mirrored_columns <= schema_columns
    assert schema_columns <= mirrored_columns


def test_high_risk_tables_have_required_columns() -> None:
    """Search, chunk, graph, and category paths need these columns present."""

    for table, expected_columns in REQUIRED_SCHEMA_COLUMNS.items():
        schema_columns = _columns_from_schema(table)
        missing = set(expected_columns) - schema_columns
        assert not missing, f"{table} missing columns: {sorted(missing)}"
