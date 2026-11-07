"""What a key becoming 'high' leaves behind on the tables search reads.

A 'high' row is kept out of every search leg by having no live vector. Each path that
makes a key high retires the vectors the key had (`retire_vectors_for_keys`; under the
Postgres authority one that failed is left to the delivery worker, the fence hiding
the served model's vector meanwhile) and calls `fence_high`: a tombstoned, vector-less
memory_embeddings row at the high row's version (an older write's vector still in
flight lands below it), and tombstones for the key's verbatim memory.set chunks, which
the chunk leg reads without a join back to loom.memory.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from server.core import pg
from server.core.error_summary import error_summary
from server.core.errors import LoomServerError
from server.core.shielded import shielded
from server.embedding import get_embedder
from server.memory.app.drops import record_drop
from server.memory.app.set_chunks import tombstone_set_chunks
from server.memory.store import pg_memory
from server.memory.store.rows import IdentityKey

log = logging.getLogger(__name__)


def _unfenced_item(key: IdentityKey) -> str:
    """A failed fence's drop-ledger item: the key as agent/session/memory_key. No user id,
    which a user erasure could not take back out of the ledger."""
    _org, _namespace, agent, _user_id, session_id, memory_key = key
    return f"{agent}/{session_id}/{memory_key}"


class CommitAnswer:
    """Whether the write's commit is known to be in (`fenced_once_committed`)."""

    def __init__(self) -> None:
        self.committed = False

    def mark_committed(self) -> None:
        self.committed = True


def may_have_landed(e: BaseException) -> bool:
    """Whether a Postgres write that ended in `e` may be in: `pg.may_have_committed`, or a
    write's own outcome_unknown 503 (a restore's re-read)."""
    return pg.may_have_committed(e) or (
        isinstance(e, LoomServerError) and bool(e.extra.get("outcome_unknown"))
    )


@contextlib.asynccontextmanager
async def fenced_once_committed(
    items: Callable[[], list[tuple[IdentityKey, int]]], *, op: str, ch: Any = None
) -> AsyncIterator[CommitAnswer]:
    """Fences a Postgres write's 'high' rows once as the block ends, if its commit answered
    or may have landed; `items` is read then, so a lifted version is the one fenced. A
    failed fence is recorded under `op`. On ClickHouse it does nothing."""
    answer = CommitAnswer()
    if not pg.enabled():
        yield answer
        return
    try:
        yield answer
    except BaseException as e:
        if answer.committed:
            await _fence_apart(items(), fence_committed, op=op, ch=ch)
        elif may_have_landed(e):
            await _fence_apart(items(), _settled_fence, op=op, ch=ch)
        raise
    if answer.committed:
        await _fence_apart(items(), fence_committed, op=op, ch=ch)


async def _fence_apart(
    items: list[tuple[IdentityKey, int]],
    fence: Callable[..., Coroutine[Any, Any, Any]],
    *,
    op: str,
    ch: Any,
) -> None:
    """`fence(items)` run to its end however often the request is cancelled meanwhile
    (anyio cancels again at every await), named for the shutdown drain's log."""
    if items:
        org, namespace = items[0][0][:2]
        name = f"{op}.fence {org}/{namespace} ({len(items)} keys)"
        await shielded(fence(items, op=op, ch=ch), name=name)


async def _settled_fence(items: list[tuple[IdentityKey, int]], *, op: str, ch: Any) -> None:
    """A commit whose answer was lost, fenced where its row is the key's head once the
    commit settles, or may be: a key with no head may hold a first write still in flight,
    which no read can wait for. A head below it means it did not land, and one above it
    that a later write owns the key. When the heads cannot be read every row is fenced
    and recorded: one that did not land hides its key's previous head until the key is
    written again."""
    try:
        heads = await pg_memory.settled_head_versions([key for key, _version in items])
    except Exception as e:  # noqa: BLE001 — unread, every row may be in: all are fenced
        log.warning("%s: a lost commit's heads unread, every row fenced: %s", op, error_summary(e))
        if await fence_committed(items, op=op, confirmed=False, ch=ch):
            for key, _version in items:
                record_drop(key[0], key[1], op, "fenced_outcome_unknown", item=_unfenced_item(key))
        return
    held = [(key, v) for key, v in items if key not in heads or heads[key] == v]
    if held:
        await fence_committed(held, op=op, confirmed=False, ch=ch)


async def fence_committed(
    items: list[tuple[IdentityKey, int]], *, op: str, confirmed: bool = True, ch: Any = None
) -> bool:
    """`fence_high` for rows that are in or may be in, and whether it held: a failure is
    recorded under `op` (`fence_failed_outcome_unknown` when not `confirmed`), never the
    write's answer."""
    try:
        await fence_high(items, ch=ch)
    except Exception as e:  # noqa: BLE001 — the written rows' answer stands
        reason = "fence_failed" if confirmed else "fence_failed_outcome_unknown"
        log.warning("%s: a written row's fence failed (%s): %s", op, reason, error_summary(e))
        for key, _version in items:
            record_drop(key[0], key[1], op, reason, item=_unfenced_item(key))
        return False
    return True


async def fence_high(items: list[tuple[IdentityKey, int]], *, ch: Any = None) -> None:
    """`items` are (identity key, version of the high memory row). Call it after the
    high row is in: a chunk writer still embedding re-reads the head after its insert,
    and only a head already labelled high makes it retire its own chunks."""
    import server.memory.store as memory_repo

    if not items:
        return
    await memory_repo.fence_vectors(items, model=get_embedder().model_name, ch=ch)
    await tombstone_chunks_of([key for key, _version in items], ch=ch)
    await recheck_live_vectors([key for key, _version in items], ch=ch)


async def recheck_live_vectors(keys: list[IdentityKey], *, ch: Any = None) -> None:
    """A vector written above the fence by a pass that drew its version before its
    scan but after this write drew its own (dedup, reconcile-status) would stay: the
    keys' live vectors go through the same head re-check a copy runs."""
    import server.memory.store as memory_repo
    from server.memory.store import read_your_writes_enabled

    try:
        live = await memory_repo.live_rows_for_keys(
            keys, ch=ch, sequential=read_your_writes_enabled()
        )
        await memory_repo.retire_copies_under_unembedded_heads(live, ch=ch)
    except Exception as e:  # noqa: BLE001 — the high row and its fence are in; a missed re-check is logged
        log.warning(
            "fence_high: live-vector re-check failed for %d keys: %s", len(keys), error_summary(e)
        )


async def tombstone_chunks_of(keys: list[IdentityKey], *, ch: Any = None) -> None:
    """Tombstone each key's live memory.set chunks, read with sequential consistency
    so a chunk insert another replica took is seen."""
    for key in keys:
        await tombstone_set_chunks(
            org=key[0], namespace=key[1], memory_key=key[5], user_id=key[3], ch=ch, strong=True
        )
