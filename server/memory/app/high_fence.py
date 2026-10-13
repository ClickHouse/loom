"""What a key becoming 'high' leaves behind on the tables search reads.

A 'high' row is kept out of every search leg by having no live vector. Each
path that makes a key high retires the vectors the key had
(`retire_vectors_for_keys`) and then calls `fence_high`: a tombstoned,
vector-less memory_embeddings row at the high row's version (an older write's
vector still in flight lands below it), and tombstones for the key's verbatim
memory.set chunks, which the chunk leg reads without a join back to
loom.memory.
"""

from __future__ import annotations

from typing import Any

from server.embedding import get_embedder
from server.memory.app.set_chunks import tombstone_set_chunks


async def fence_high(items: list[tuple[Any, int]], *, ch: Any = None) -> None:
    """`items` are (identity key, version of the high memory row). Call it after the
    high row is in: a chunk writer still embedding re-reads the head after its insert,
    and only a head already labelled high makes it retire its own chunks."""
    import server.memory.store as memory_repo

    if not items:
        return
    await memory_repo.fence_vectors(items, model=get_embedder().model_name, ch=ch)
    await tombstone_chunks_of([key for key, _version in items], ch=ch)


async def tombstone_chunks_of(keys: list[Any], *, ch: Any = None) -> None:
    """Tombstone each key's live memory.set chunks, read with sequential consistency
    so a chunk insert another replica took is seen."""
    for key in keys:
        await tombstone_set_chunks(
            org=key[0], namespace=key[1], memory_key=key[5], user_id=key[3], ch=ch, strong=True
        )
