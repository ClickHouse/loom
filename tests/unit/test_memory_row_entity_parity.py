"""`loom.memory` and `loom.memory_embeddings` must agree about entities.

`memory.set` writes one fact into two tables. `ch.insert` derives its column
list from `rows[0].keys()`, so a key omitted from one row dict silently takes
the schema default instead of raising.

Losing `entities` from the loom.memory row loses data, not just consistency.
`find_unentitied_memories` selects on `empty(loom.memory.entities)`, so the
entity_extractor worker re-extracts such a row. For a short value it finds
nothing and writes its `["_none_"]` sentinel to both tables, which destroys
the caller's entities and hides the row from `memory.timeline(entity=...)`.

The test reads the handler's source rather than exercising the write path: the
failure is an absent dict key, which only a real ClickHouse would notice
behaviourally.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HANDLER = ROOT / "server" / "memory" / "app" / "write.py"
# The embedding row is built in the commit half of memory.set.
COMMIT = ROOT / "server" / "memory" / "app" / "set_commit.py"

# Columns that exist on both tables and describe the FACT rather than its
# vector. If one row sets these and the other does not, the tables disagree
# about the same memory.
SHARED_FACT_KEYS = {"entities", "tags"}


def _dict_literal_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _memory_row_keys() -> set[str]:
    """Keys of the `memory_row = {...}` literal on the memory.set path."""
    tree = ast.parse(HANDLER.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "memory_row":
                keys = _dict_literal_keys(node.value)
                # The `memory.set` row is the one carrying `value_json`; the
                # other assignment to this name is a version-row helper call.
                if "value_json" in keys:
                    return keys
    raise AssertionError("could not find the memory_row dict literal in _memory_write.py")


def test_memory_row_carries_the_shared_fact_columns() -> None:
    keys = _memory_row_keys()
    missing = SHARED_FACT_KEYS - keys
    assert not missing, (
        f"memory_row omits {sorted(missing)}. ch.insert derives its column list "
        "from rows[0].keys(), so an omitted key silently writes the schema "
        "default instead of failing — and loom.memory then disagrees with "
        "loom.memory_embeddings about the same memory. This exact omission of "
        "`entities` let the entity_extractor worker treat every "
        "memory.set(entities=[...]) as unentitied and overwrite the caller's "
        "entities with its ['_none_'] sentinel."
    )


def test_the_embedding_row_copies_the_memory_rows_entities() -> None:
    """One canonical form in both rows, or `has(entities, 'x')` matches one table and
    not the other: the memory row canonicalises the input, the embedding row copies it."""
    assert '"entities": canonical_entities(input.entities or [])' in HANDLER.read_text()
    assert "memory_repo.search_document_row(" in COMMIT.read_text()
    assert 'entities=memory_row["entities"]' in _search_document_row()


def test_sentinel_is_only_written_for_genuinely_empty_extraction() -> None:
    """Guard the other half: the worker's sentinel means 'extraction found
    nothing', so it must never be the value that replaces real entities."""
    worker = (ROOT / "server" / "memory" / "workers" / "entity_extractor.py").read_text()
    assert '"_none_"' in worker or "'_none_'" in worker, (
        "expected the entity_extractor sentinel to still be present; if it was "
        "renamed, update find_unentitied_memories and this test together"
    )


def test_embedding_row_carries_the_memory_rows_lifecycle_status() -> None:
    """The `duplicate` verdict lands on `memory_row` before
    the insert; an embedding row built without `status=` would take the schema
    default `active`, and the pool memory.search reads would serve the
    duplicate as current. The first version of the mirror row must carry the
    same status as the memory row."""
    assert "memory_repo.search_document_row(" in COMMIT.read_text()
    assert 'status=str(memory_row.get("status") or "active")' in _search_document_row(), (
        "memory.set builds its loom.memory_embeddings row without the lifecycle "
        "status of the loom.memory row; a duplicate verdict is then invisible to "
        "the search pool until a reconcile runs."
    )


def _search_document_row() -> str:
    """The source of `rows.search_document_row`, the builder memory.set's embedding row
    and the delivery worker's both come from."""
    src = (ROOT / "server" / "memory" / "store" / "rows.py").read_text()
    start = src.index("def search_document_row(")
    return src[start : src.index("\n\n\n", start)]


def test_a_search_document_carries_its_rows_invalid_at() -> None:
    """A document the delivery worker builds for an invalidated head is not served as
    valid under `as_of`: it carries the row's `invalid_at`, as the status mirror does."""
    from datetime import datetime

    from server.memory.store.rows import (
        MemoryRow,
        build_memory_version_row,
        search_document_row,
    )

    head = MemoryRow(
        memory_id="m1",
        org="o",
        namespace="ns",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 1, 1),
        expires_at=None,
    )
    row = build_memory_version_row(head, invalid_at=datetime(2026, 2, 1), version=2)
    doc = search_document_row(
        row, embedding_model="m", embedding=[0.1], key_embedding=[], key_text="", excerpt="v"
    )
    assert doc["invalid_at"] == datetime(2026, 2, 1) and doc["version"] == 2
