"""Table settings Loom needs from any ClickHouse, carried by the tables
themselves rather than by server config (028_table_settings.sql)."""

from __future__ import annotations

from tests.unit.schema_ddl import ddl, tables


def _settings(name: str) -> str:
    return next(line for line in ddl(name).splitlines() if line.startswith("SETTINGS "))


def test_every_merge_tree_table_keeps_insert_dedup_tokens() -> None:
    merge_trees = [t for t in sorted(tables()) if "MergeTree" in ddl(t)]
    assert len(merge_trees) > 100
    missing = [
        t for t in merge_trees if "non_replicated_deduplication_window = 1000" not in _settings(t)
    ]
    assert not missing, f"add `non_replicated_deduplication_window = 1000` to: {missing}"


def test_the_lightweight_update_tables_carry_block_columns() -> None:
    for name in ("memory", "memory_embeddings"):
        line = _settings(name)
        assert "enable_block_number_column = 1" in line
        assert "enable_block_offset_column = 1" in line
