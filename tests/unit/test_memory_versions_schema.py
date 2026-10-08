"""loom.memory_versions must mirror loom.memory column-for-column, and the
materialized view must copy every column — otherwise the history silently
stops recording a field. Read from schemas/schema.sql, the
canonical dump."""

from __future__ import annotations

import re

from server.memory.store import MEMORY_PERSISTED_COLS
from tests.unit.schema_ddl import columns, ddl, engine, ttl, view_select


def test_versions_table_has_every_memory_column_with_the_same_type() -> None:
    memory = columns("memory")
    versions = columns("memory_versions")
    missing = sorted(set(memory) - set(versions))
    assert not missing, f"loom.memory_versions lacks {missing}: add to the table + view"
    drift = {c: (memory[c], versions[c]) for c in memory if memory[c] != versions[c]}
    assert not drift, f"type drift between loom.memory and loom.memory_versions: {drift}"


def test_versions_table_covers_the_row_builder_columns() -> None:
    versions = columns("memory_versions")
    missing = sorted(set(MEMORY_PERSISTED_COLS) - set(versions))
    assert not missing, missing


def test_row_builder_writes_every_stored_memory_column() -> None:
    # A version row that omits a column gets its DDL default, and FINAL then
    # serves that default over the previous version's value.
    stored = set(columns("memory", materialized=False))
    assert set(MEMORY_PERSISTED_COLS) == stored, (
        f"builder lacks {sorted(stored - set(MEMORY_PERSISTED_COLS))}, "
        f"schema lacks {sorted(set(MEMORY_PERSISTED_COLS) - stored)}"
    )


def test_view_selects_every_memory_column() -> None:
    assert (
        "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.memory_versions_mv TO loom.memory_versions\n"
        in ddl("memory_versions_mv")
    )
    m = re.fullmatch(r"SELECT(.*?)FROM loom\.memory", view_select("memory_versions_mv"), re.S)
    assert m, "memory_versions_mv does not select from loom.memory"
    selected = {tok.strip().strip("`") for tok in m.group(1).replace("\n", " ").split(",")}
    # A MATERIALIZED column is computed by the target table on every insert
    # (value_preview / value_len); the view cannot carry it.
    memory = set(columns("memory", materialized=False))
    assert memory <= selected, f"view does not copy {sorted(memory - selected)}"


def test_versions_sorting_key_ends_in_version_so_distinct_writes_never_collapse() -> None:
    m = re.search(r"^ORDER BY \((.*?)\)", ddl("memory_versions"), re.M)
    assert m and m.group(1).split(",")[-1].strip() == "version"
    assert engine("memory_versions") == "ReplacingMergeTree"


def test_versions_table_has_no_ttl() -> None:
    # The history exists to be rolled back to; erasure removes it, time must not.
    assert ttl("memory_versions") is None
