"""Table settings Loom needs from any ClickHouse, carried by the tables
themselves rather than by server config (031_table_settings.sql)."""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.schema_ddl import ddl, tables

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "schemas" / "migrations"
SHAPES = ROOT / "tests" / "analyzer" / "shapes.txt"


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
    # Every table Loom runs a lightweight UPDATE on, from the statement corpus:
    # without both columns the UPDATE quietly becomes a mutation per call.
    updated = sorted(set(re.findall(r"\bUPDATE loom\.(\w+) SET\b", SHAPES.read_text())))
    assert updated
    for name in updated:
        line = _settings(name)
        assert "enable_block_number_column = 1" in line, name
        assert "enable_block_offset_column = 1" in line, name


def test_the_tables_the_dump_leaves_out_keep_insert_dedup_tokens_too() -> None:
    # The schema dump skips `_`-prefixed tables (the migrator's own, the provider
    # participants); read them from the migrations: the window in the CREATE, or
    # set by an ALTER.
    sql = "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")))
    creates = dict(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS loom\.(_\w+)\s*(\(.*?\)\s*ENGINE\s*=\s*\w*MergeTree.*?);",
            sql,
            re.S,
        )
    )
    assert creates
    window = "non_replicated_deduplication_window = 1000"
    missing = [
        t
        for t, body in creates.items()
        if window not in body and f"ALTER TABLE loom.{t} MODIFY SETTING {window}" not in sql
    ]
    assert not missing, f"set the window on {missing}"
