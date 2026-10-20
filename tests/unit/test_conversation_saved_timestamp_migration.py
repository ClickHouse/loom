"""Saved turns remain listable when the timestamp column is added."""

from pathlib import Path

import pytest

from tests.unit.clickhouse_local import CLICKHOUSE, run

pytestmark = pytest.mark.skipif(not CLICKHOUSE, reason="clickhouse binary not installed")


@pytest.mark.parametrize("table", ["conversations", "conversation_embeddings"])
def test_saved_timestamp_migration_preserves_existing_saves(table: str) -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "schemas/migrations/060_conversation_saved_at.sql"
    ).read_text()
    setup = []
    for name in ("conversations", "conversation_embeddings"):
        setup.extend(
            [
                f"CREATE TABLE loom.{name} (msg_id String, is_saved Bool, "
                "ts DateTime64(3, 'UTC')) ENGINE MergeTree ORDER BY msg_id",
                f"INSERT INTO loom.{name} VALUES "
                "('saved', true, '2026-01-01 00:00:00.123'),"
                "('ordinary', false, '2026-01-01 00:00:00.456')",
            ]
        )
    setup.append(migration)
    rows = run(
        setup, f"SELECT msg_id, toString(saved_at) AS saved_at FROM loom.{table} ORDER BY msg_id"
    )
    assert rows == [
        {"msg_id": "ordinary", "saved_at": None},
        {"msg_id": "saved", "saved_at": "2026-01-01 00:00:00.123"},
    ]
