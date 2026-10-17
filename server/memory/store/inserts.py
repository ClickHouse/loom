"""The committed insert funnels for loom.memory and loom.memory_embeddings,
and the full-row check the loom.memory one applies."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from typing import Any, cast

from server.core.errors import PreconditionFailedError
from server.core.repo_base import _CHClient, _default_client
from server.memory.store.rows import MEMORY_PERSISTED_COLS
from server.settings import settings

log = logging.getLogger(__name__)

# Runs on every loom.memory batch before it is inserted, may refuse it, and returns
# the rows to insert: the temporal publisher (server/wiring.py registers it)
# journals the change first, and leaves out a retry's row its key has moved past.
Rows = Sequence[Mapping[str, Any]]
_BEFORE_INSERT: list[Callable[..., Awaitable[Rows]]] = []


class Handoff:
    """Work a before-insert hook hands back to run alongside the insert rather than
    ahead of it (the journal's projection, which readers never need ahead of the
    row). Open only while the hooks run: a task they spawned that hands work over
    later is told to run it itself."""

    def __init__(self) -> None:
        self.work: list[Awaitable[Any]] = []
        self.open = True

    def take(self, work: Awaitable[Any]) -> bool:
        if self.open:
            self.work.append(work)
        return self.open


alongside_insert: ContextVar[Handoff | None] = ContextVar("alongside_insert", default=None)


# Runs on every loom.memory batch once its insert returned, even with an error (it
# may have landed), and may take it back out (the publisher re-checks the erasure
# fence) and refuse the write.
_AFTER_INSERT: list[Callable[..., Awaitable[None]]] = []


def before_memory_insert(fn: Callable[..., Awaitable[Rows]]) -> None:
    """Register `fn(rows, ch=...) -> rows to insert` once."""
    if fn not in _BEFORE_INSERT:
        _BEFORE_INSERT.append(fn)


def after_memory_insert(fn: Callable[..., Awaitable[None]]) -> None:
    """Register `fn(rows, ch=...)` once."""
    if fn not in _AFTER_INSERT:
        _AFTER_INSERT.append(fn)


def _ident(r: Mapping[str, Any]) -> tuple[Any, ...]:
    return (r["agent"], r["user_id"], r["session_id"], r["memory_key"])


def kept_embedding_rows(
    kept: Rows,
    embedding_rows: Sequence[Mapping[str, Any]],
    drawn: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The embedding rows of the version rows the hooks kept, at the version and
    with the expiry those were given (stamped by the publisher; a retry's are its
    commit's). A row the journal left unembedded (high, or a type never embedded)
    keeps no vector: its embedding row is dropped. `drawn` (version drawn -> its row)
    matches each embedding row to its own row, for a batch with several of one key."""
    from server.memory.domain.memory_types import is_embedded

    kept_ids = {id(r) for r in kept}
    by_ident = {_ident(r): r for r in kept}

    def row_of(e: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if drawn is None:
            return by_ident.get(_ident(e))
        row = drawn.get(int(e.get("version") or 0))
        return row if row is not None and id(row) in kept_ids else None

    out = []
    for e in embedding_rows:
        row = row_of(e)
        if row is not None and is_embedded(
            str(row.get("memory_type")), str(row.get("sensitivity") or "")
        ):
            out.append({**e, "version": row["version"], "expires_at": row.get("expires_at")})
    return out


def check_memory_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a loom.memory row that does not carry every persisted column.

    `ch.insert` derives the column list from the first row and ClickHouse
    fills the rest with DDL defaults, which FINAL then serves over the prior
    version's real values — the failure CLAUDE.md's RMT rule describes. An
    unknown key is refused too: a typo'd column name is a bug, not a dropped
    value."""
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
    alongside: Sequence[Callable[[Rows], Awaitable[Any]]] = (),
) -> int:
    """The one loom.memory write: every row is checked against
    MEMORY_PERSISTED_COLS before the insert (see `check_memory_rows`), and
    the insert is committed. `alongside` (the rows' embedding writes) runs
    concurrently with the insert, only once the hooks accepted the batch, and is
    handed the rows they kept: a refused batch leaves nothing searchable.
    Returns the rows inserted."""
    if not rows:
        return 0
    check_memory_rows(rows)
    if not settings.memory_bitemporal_enabled:
        # No journal: the row and its embedding writes land together, as they did
        # before the journal existed.
        db = ch or _default_client()
        await asyncio.gather(
            db.insert(
                "loom.memory",
                [dict(r) for r in rows],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            ),
            *(f(rows) for f in alongside),
        )
        return len(rows)
    # A cancelled request must leave neither a commit without its row nor rows
    # unfenced: the journal hook, the insert and the after-insert fence finish
    # together in their own task whatever happens to the caller.
    task = asyncio.ensure_future(_publish_insert_fence(rows, ch or _default_client(), alongside))
    _FENCING.add(task)
    task.add_done_callback(_fenced)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_report_abandoned)  # nobody else will see its outcome
        raise


# Insert-and-fence tasks a cancelled caller left running (the loop holds tasks weakly).
_FENCING: set[asyncio.Future[int]] = set()


async def _publish_insert_fence(
    rows: Rows, ch: _CHClient, alongside: Sequence[Callable[[Rows], Awaitable[Any]]]
) -> int:
    handoff = Handoff()
    token = alongside_insert.set(handoff)
    try:
        for hook in _BEFORE_INSERT:
            rows = await hook(rows, ch=ch)
    except BaseException:
        await asyncio.gather(*handoff.work, return_exceptions=True)  # never left unawaited
        raise
    finally:
        handoff.open = False
        alongside_insert.reset(token)
    handed = handoff.work
    if not rows:
        await asyncio.gather(*handed)
        return 0
    # Each row names the version this build wrote it at, once the journal has stamped
    # it: a build that does not journal carries the column forward unchanged.
    for r in rows:
        cast(dict[str, Any], r)["journal_version"] = int(r.get("version") or 0)
    await _insert_then_fence(rows, ch, alongside, handed)
    return len(rows)


def _fenced(task: asyncio.Future[int]) -> None:
    _FENCING.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved: a caller that was cancelled never awaits it


def _report_abandoned(task: asyncio.Future[int]) -> None:
    e = None if task.cancelled() else task.exception()
    if isinstance(e, PreconditionFailedError):
        log.info("a cancelled write's rows were taken back out by the erasure fence")
    elif e is not None or task.cancelled():
        log.error("a cancelled write's loom.memory insert or its erasure fence failed: %r", e)


async def drain(timeout_s: float) -> None:
    """Let the inserts cancelled requests left running finish, fence included."""
    if _FENCING:
        await asyncio.wait(set(_FENCING), timeout=timeout_s)


async def _insert_then_fence(
    rows: Rows,
    ch: _CHClient,
    alongside: Sequence[Callable[[Rows], Awaitable[Any]]],
    handed: Sequence[Awaitable[Any]] = (),
) -> None:
    # Handed work runs from the insert's start until the after-insert hooks are done.
    side = [asyncio.ensure_future(w) for w in handed]
    try:
        outcomes = await asyncio.gather(
            ch.insert(
                "loom.memory",
                [dict(r) for r in rows],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            ),
            *(f(rows) for f in alongside),
            return_exceptions=True,
        )
        # Whatever failed, the rows may be in: the hooks' fence holds for them first.
        for after in _AFTER_INSERT:
            await after(rows, ch=ch)
    finally:
        outcomes_side = await asyncio.gather(*side, return_exceptions=True)
    for outcome in [*outcomes, *outcomes_side]:
        if isinstance(outcome, BaseException):
            raise outcome


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
