"""The vector tables write Wide parts from 128 MiB: each carries
`min_bytes_for_wide_part = 134217728` in its CREATE, and no other table
does."""

from __future__ import annotations

from tests.unit.schema_ddl import ddl, schema_text

# Cloud's min_bytes_for_full_part_storage: below it a part is one packed
# object whatever its type, so Wide adds no objects there.
WIDE_FROM = 134217728
TABLES = (
    "memory_embeddings",
    "memory_chunks",
    "code_chunks",
    "code_chunks_prefix",
    "entity_vectors",
    "session_summaries",
)


def test_schema_dump_carries_the_threshold_for_the_vector_tables_only() -> None:
    for table in TABLES:
        settings = ddl(table).rsplit("\nSETTINGS ", 1)[1]
        assert f"min_bytes_for_wide_part = {WIDE_FROM}" in settings, (table, settings)
    assert schema_text().count("min_bytes_for_wide_part") == len(TABLES)
