"""A migration statement that reads loom tables is sent
with select_sequential_consistency = 1; DDL and plain INSERT VALUES are not."""

from __future__ import annotations

from server.core.schema.migrate import _consistency_settings, _reads_loom_table


def test_insert_select_from_loom_table_reads_sequentially() -> None:
    stmt = (
        "INSERT INTO loom.namespaces SELECT * REPLACE ('private' AS visibility) "
        "FROM loom.namespaces FINAL WHERE kind = 'code'"
    )
    assert _reads_loom_table(stmt)
    assert _consistency_settings(stmt) == {"select_sequential_consistency": 1}


def test_alter_update_with_subselect_reads_sequentially() -> None:
    stmt = "ALTER TABLE loom.memory UPDATE status = 'x' WHERE memory_key IN (SELECT memory_key FROM loom.memory_drops)"
    assert _reads_loom_table(stmt)


def test_ddl_and_values_insert_carry_no_consistency_setting() -> None:
    for stmt in (
        "CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = MergeTree ORDER BY a",
        "ALTER TABLE loom.x ADD COLUMN IF NOT EXISTS b String DEFAULT ''",
        "INSERT INTO loom.x (a) VALUES (1)",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.mv TO loom.x AS SELECT a FROM loom.y",
    ):
        assert _consistency_settings(stmt) == {}, stmt
