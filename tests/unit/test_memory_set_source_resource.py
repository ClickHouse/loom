"""memory.set writes `source_resource_id` on the loom.memory row, not only on its mirror.

Every later mirror (update, verify, delete, restore) copies the column from the
loom.memory row, and memory.provenance reads it there: a row without it would keep
the caller's value on the embedding only until the first rewrite blanked it."""

from __future__ import annotations

from server.memory.api.models_crud import MemorySetInput
from server.memory.app.set_commit import _SetWrite
from server.memory.app.write import _set_build_memory_row
from server.memory.store import MEMORY_PERSISTED_COLS


def test_the_memory_row_carries_the_callers_source_resource() -> None:
    w = _SetWrite(
        input=MemorySetInput(
            org="o",
            namespace="n",
            agent="a",
            memory_key="k",
            value="v",
            source_resource_id="doc-9",
        ),
        version=1,
        retyped_from="",
        existing=None,
        expires_at=None,
        serialized_value='"v"',
    )
    _set_build_memory_row(w)
    assert "source_resource_id" in MEMORY_PERSISTED_COLS
    assert w.memory_row["source_resource_id"] == "doc-9"
