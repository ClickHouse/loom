"""The committed insert funnels for loom.memory and loom.memory_embeddings,
and the full-row check the loom.memory one applies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.store.rows import MEMORY_PERSISTED_COLS


def check_memory_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a loom.memory row that does not carry every persisted column.

    `ch.insert` derives the column list from the first row and ClickHouse
    fills the rest with DDL defaults, which FINAL then serves over the prior
    version's real values, and the omitted column reads as its default for
    everyone. An unknown key is refused too: a typo'd column name is a bug, not
    a dropped value."""
    wanted = set(MEMORY_PERSISTED_COLS)
    for i, row in enumerate(rows):
        keys = set(row)
        missing = sorted(wanted - keys)
        unknown = sorted(keys - wanted)
        if missing or unknown:
            key = row.get("memory_key", "?")
            raise ValueError(
                f"loom.memory row #{i} ({key!r}) is not a full version row: "
                f"missing={missing} unknown={unknown}"
            )


async def insert_memory_versions(
    rows: Sequence[Mapping[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> int:
    """The one loom.memory write: every row is checked against
    MEMORY_PERSISTED_COLS before the insert (see `check_memory_rows`), and
    the insert is committed. Returns the row count; an empty batch writes
    nothing."""
    if not rows:
        return 0
    check_memory_rows(rows)
    ch = ch or _default_client()
    await ch.insert(
        "loom.memory",
        [dict(r) for r in rows],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return len(rows)


async def insert_embedding_rows(
    rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert N memory_embedding rows, committed.

    Synchronous because this is the row that makes a memory findable
    (`memory.search`, `memory.timeline` and `knowledge.search` read it): a
    fire-and-forget insert would let `memory.set` return before its memory
    can be found. The cost is tens of milliseconds per write.

    Empty `rows` is a no-op.
    """
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(
        "loom.memory_embeddings",
        rows,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
