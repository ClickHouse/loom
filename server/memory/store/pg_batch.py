"""Publication of committed revisions in batches, under the Postgres authority.

A commit hands its rows, and the search documents its writer built, to this process's
batch (`submit`). One flush sends every commit queued since the last one: one insert into
loom.memory, one into loom.audit_log and one into loom.memory_embeddings, whatever their
namespaces and orgs, then clears every delivered target in one Postgres transaction. So
ClickHouse gets parts at the flush rate, not the write rate.

Flushes start at most 1000 / `pg_publish_interval_ms` a second on average, up to a
second's worth at once (`_wait_for_a_start`): a process writing now and then sends each
write at once, and a busy one sends at most that many inserts a second into each table.
Each commit is sent under its own lease (`Committed.deadline`, `COMMIT_TOKEN`), so the
fencing an erasure or a redaction relies on is the writer's, as when it published alone.
What ClickHouse refuses is sent again commit by commit; a flush it did not answer, and a
commit whose lease ran out in the queue, are left to the delivery worker.
storage-authority.md, "Publication".
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core import pg, pg_lease
from server.core.ch import set_query_tag
from server.core.ch_errors import is_connection_failure, is_passing_state
from server.core.ch_writes import partition_blocks
from server.core.error_summary import error_summary
from server.core.repo_base import _CHClient, _default_client
from server.memory.store import inserts, pg_memory, pg_publish
from server.memory.store.pg_memory import AUDIT_ROW, MEMORY_ROW, SEARCH_DOC, Committed
from server.memory.store.rows import created_month, identity_key, search_document_row
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    committed: Committed
    # The search documents the writer built for these rows, at their versions.
    documents: list[dict[str, Any]]
    retried: bool
    ch: _CHClient
    # Resolved once the entry is published, or with why it is not.
    done: asyncio.Future[None]
    # What an earlier send of this entry already landed: a retry sends only the rest.
    rows_sent: bool = False
    documents_sent: bool = False
    # The deliveries of documents not sent because their key's head moved on.
    stale: list[tuple[str, str, int]] = field(default_factory=list)
    # Why the entry was left to the delivery worker; None: published.
    error: BaseException | None = None


@dataclass
class _Batch:
    queue: list[_Entry] = field(default_factory=list)
    # The entries of the flush in progress.
    sending: list[_Entry] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    # The flushes that may start at once, and when they were last counted
    # (`_wait_for_a_start`).
    tokens: float = math.inf  # full: capped at the bucket's size when first counted
    refilled: float = field(default_factory=time.monotonic)


# One batch per event loop: a flush's task and futures belong to the loop that made them.
_batches: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Batch] = weakref.WeakKeyDictionary()


# The publication of the write this request answered at its commit, and its lease's end.
_own_write: contextvars.ContextVar[tuple[asyncio.Future[None], float] | None] = (
    contextvars.ContextVar("pg_batch_own_write", default=None)
)


def _batch() -> _Batch:
    loop = asyncio.get_running_loop()
    batch = _batches.get(loop)
    if batch is None:
        batch = _batches[loop] = _Batch()
    return batch


async def submit(
    committed: Committed,
    *,
    documents: Sequence[Mapping[str, Any]] = (),
    retried: bool = False,
    ch: _CHClient | None = None,
    wait: bool = True,
) -> None:
    """Queue a commit's publication. With `wait`, return once it is published, or raise
    PublicationPending when it is not by its lease's end. Without, return at once; the
    request's graph fences wait for it (`own_write_published`)."""
    loop = asyncio.get_running_loop()
    entry = _Entry(
        committed,
        [dict(d) for d in documents],
        retried,
        ch or _default_client(),
        loop.create_future(),
    )
    batch = _batch()
    batch.queue.append(entry)
    if batch.task is None or batch.task.done():
        # A context of its own: no request's quota key, query tag or budget is charged
        # for the flushes of every org.
        batch.task = loop.create_task(_run(batch), context=contextvars.Context())
    if not wait:
        _own_write.set((entry.done, committed.deadline))
        return
    # A cancelled or timed-out caller leaves its entry queued: the commit stands, and the
    # flush sends it while its lease holds.
    left = committed.deadline - time.monotonic()
    try:
        await asyncio.wait_for(asyncio.shield(entry.done), max(left, 0.0))
    except TimeoutError:
        raise pg_lease.PublicationPending() from None


async def own_write_published() -> None:
    """Wait until the write this request answered at its commit is published, or its lease
    runs out: what a read of ClickHouse that must see that write (a graph fence) waits
    for. With none pending, returns at once; never raises."""
    own = _own_write.get()
    if own is None:
        return
    done, deadline = own
    if not done.done():
        await asyncio.wait([done], timeout=max(deadline - time.monotonic(), 0.0))


async def drain(timeout_s: float) -> None:
    """Wait, at most `timeout_s`, for this loop's queued commits to be sent: at shutdown.
    What is left is the delivery worker's once its lease runs out."""
    batch = _batches.get(asyncio.get_running_loop())
    if batch is None or batch.task is None or batch.task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(batch.task), timeout_s)
    except TimeoutError:
        left = len(batch.queue) + len(batch.sending)
        log.warning("pg_batch: %d commits left to the delivery worker", left)
    except (Exception, asyncio.CancelledError) as e:
        if not batch.task.done():
            raise  # the drain itself was cancelled
        # The flush ended so: it failed its commits already (`_run`), and logged why.
        log.warning("pg_batch: drain ended with the flush: %s", error_summary(e))


def _wait_for_a_start(batch: _Batch) -> float:
    """Seconds before the next flush may start, by a token bucket: flushes start at most
    1000 / `pg_publish_interval_ms` a second on average, and up to a second's worth (at
    least one) at once, so a process that writes now and then sends each write at once."""
    interval_ms = settings.pg_publish_interval_ms
    if settings.pg_projection_wait_ms > 0:
        # A read right after a write waits at the barrier for its flush: the pace leaves
        # it at least half its wait (a live change is not checked against the other).
        interval_ms = min(interval_ms, settings.pg_projection_wait_ms // 2)
    if interval_ms <= 0:
        batch.tokens = math.inf  # no pace: a later one starts with a full bucket
        return 0.0
    rate = 1000 / interval_ms  # flushes a second
    now = time.monotonic()
    batch.tokens = min(max(rate, 1.0), batch.tokens + max(now - batch.refilled, 0.0) * rate)
    batch.refilled = now
    return 0.0 if batch.tokens >= 1 else (1 - batch.tokens) / rate


async def _run(batch: _Batch) -> None:
    """Flush until the queue is empty, one flush at a time, no more often than
    `_wait_for_a_start` allows."""
    set_query_tag("pg_batch.flush")
    try:
        while batch.queue:
            wait = _wait_for_a_start(batch)
            if wait > 0:
                await asyncio.sleep(wait)
            batch.sending, batch.queue = batch.queue, []
            batch.tokens -= 1
            await _flush(batch.sending)
            batch.sending = []
    except BaseException as e:
        # Cancelled (a shutdown), or a bug: nothing queued is left unanswered.
        if not isinstance(e, asyncio.CancelledError):
            log.exception("pg_batch: the flush failed; its commits go to the delivery worker")
        for entry in [*batch.sending, *batch.queue]:
            _fail(entry, e)
        batch.sending, batch.queue = [], []
        raise


def _unanswered(error: BaseException) -> bool:
    """Nothing refused a row: ClickHouse did not answer (a state that clears on its own,
    a connection, a timeout), Postgres did not, or a lease ended. Sent alone, each commit
    would fail the same way, one deadline after another."""
    return (
        is_passing_state(error)
        or is_connection_failure(error)
        or isinstance(error, TimeoutError | pg.PostgresUnavailableError)
        # A lease that ended before a send: whether one started is not known.
        or isinstance(error, pg_lease.PublicationPending)
    )


async def _flush(entries: list[_Entry]) -> None:
    """Send the entries whose lease has not run out, grouped by client, together; what
    ClickHouse refused is sent again entry by entry."""
    groups: dict[int, list[_Entry]] = {}
    for entry in _within_lease(entries):
        groups.setdefault(id(entry.ch), []).append(entry)

    async def resend(entry: _Entry) -> None:
        entry.retried = True
        await _publish([entry])
        _answer([entry])

    for group in groups.values():
        try:
            await _publish(group)
            _answer(group)
        except Exception as e:  # noqa: BLE001 — what ClickHouse refused is tried alone
            await _each_alone(group, e, resend)
    _answer(entries)
    await _hand_over(entries)


def _within_lease(entries: list[_Entry]) -> list[_Entry]:
    """The entries whose lease has not run out; the others are failed, the delivery
    worker's to send."""
    for entry in entries:
        if time.monotonic() >= entry.committed.deadline:
            _fail(entry, TimeoutError("its lease ran out before the flush"))
    return [entry for entry in entries if entry.error is None]


async def _each_alone(
    entries: list[_Entry],
    error: BaseException,
    send: Callable[[_Entry], Awaitable[None]],
) -> None:
    """After ClickHouse refused `entries` sent together: each one still live sent alone, so
    one bad row holds back only its own commit. An error ClickHouse did not answer fails
    them all instead, since each would wait out the same deadline one after another."""
    live = [entry for entry in entries if entry.error is None]
    if len(live) <= 1 or _unanswered(error):
        for entry in live:
            _fail(entry, error)
        return
    log.warning(
        "pg_batch: ClickHouse refused %d commits together (%s); one at a time",
        len(live),
        error_summary(error),
    )
    for i, entry in enumerate(live):
        try:
            await send(entry)
        except Exception as alone:  # noqa: BLE001 — the delivery worker takes it
            if not _unanswered(alone):
                _fail(entry, alone)
                continue
            for rest in live[i:]:
                _fail(rest, alone)
            return


def _answer(entries: list[_Entry]) -> None:
    """Tell each waiting writer whose entry landed, as soon as it has: a refused flush's
    resends do not hold back the ones already in."""
    for entry in entries:
        if entry.error is None and not entry.done.done():
            entry.done.set_result(None)


async def _hand_over(entries: list[_Entry]) -> None:
    """End the commit's lease on what this flush will not send, so the delivery worker,
    or a search waiting at its barrier, takes it at once rather than at the lease's end:
    a published entry's stale documents, and every delivery of an entry ClickHouse
    refused. Not an entry it did not answer: an insert of it may still be running."""
    seqs = [seq for e in entries for seq in _not_sent(e)]
    try:
        await pg_memory.hand_over(seqs)
    except Exception as e:  # noqa: BLE001 — the leases run out on their own
        log.warning("pg_batch: leases not handed over: %s", error_summary(e))


def _not_sent(entry: _Entry) -> list[tuple[str, str, int]]:
    """The deliveries of the entry this flush hands over."""
    if entry.error is None:
        return entry.stale  # published: only its documents whose head moved on
    if _unanswered(entry.error):
        return []  # an insert of it may still be running
    return list(entry.committed.seqs)  # refused


def _fail(entry: _Entry, error: BaseException) -> None:
    """The entry is left to the delivery worker, which takes it once its lease runs out:
    logged, and its waiting writer told its publication is pending."""
    log.warning(
        "pg_batch: a commit's publication is left to the delivery worker: %s",
        error_summary(error),
    )
    entry.error = error
    if not entry.done.done():
        entry.done.set_exception(pg_lease.PublicationPending())
        # Retrieved here too: a writer cancelled meanwhile leaves no one to read it.
        entry.done.exception()


def _doc_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """The revision a search document was built from, as its row names it."""
    return (str(row["org"]), str(row["namespace"]), *pg_publish.document_id(row))


async def _served_documents(
    group: list[_Entry],
) -> dict[int, dict[tuple[Any, ...], dict[str, Any]]]:
    """By entry, the documents still to send, each by the revision it was built from:
    its own while that is the key's head, else rebuilt onto a newer copy of it (a pin, a
    touch, a conflict mark) that owes no document of its own, as `pg_delivery._still_heads`
    serves one, so the writer's vector is not embedded again; the heads are then read
    again durably, since such a copy may be on the primary alone. One whose head was deleted,
    rewritten or redacted since is not sent: it would land live above that head's own
    retirement. Its delivery goes to the entry's `stale`, for the delivery worker to
    build from the head or settle (`_hand_over`)."""
    waiting = [e for e in group if not e.documents_sent]
    keys = [identity_key(d) for e in waiting for d in e.documents]
    heads = await pg_memory.heads_owing(keys) if keys else {}

    if any(_newer_copy(d, heads) for e in waiting for d in e.documents):
        heads = await pg_memory.heads_owing(keys, durable=True)

    sent: dict[int, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for e in waiting:
        sent[id(e)] = {
            _doc_key(d): out for d in e.documents if (out := _served(d, heads)) is not None
        }
        moved = {_doc_key(d) for d in e.documents} - sent[id(e)].keys()
        e.stale = [
            seq
            for seq, row in zip(e.committed.seqs, e.committed.rows, strict=True)
            if _doc_key(row) in moved
        ]
    return sent


_Heads = dict[Any, tuple[dict[str, Any], int]]


def _newer_copy(doc: Mapping[str, Any], heads: _Heads) -> bool:
    """Whether the key's head is a newer copy of the document's row
    (`pg_memory.newer_copy`) that owes no document of its own."""
    head, owed = heads.get(identity_key(doc), (None, 0))
    return head is not None and not owed & SEARCH_DOC and pg_memory.newer_copy(head, doc)


def _served(doc: dict[str, Any], heads: _Heads) -> dict[str, Any] | None:
    """The document to send for `doc` (`_served_documents`), None for none."""
    head, _ = heads.get(identity_key(doc), (None, 0))
    if head is None or head.get("redacted_at") is not None:
        return None
    if int(head["version"]) == int(doc["version"]):
        return doc
    return _onto(doc, head) if _newer_copy(doc, heads) else None


def _onto(doc: Mapping[str, Any], head: Mapping[str, Any]) -> dict[str, Any]:
    """The writer's document, with its vectors, carried onto a newer copy of its row."""
    return search_document_row(
        head,
        embedding_model=str(doc["embedding_model"]),
        embedding=list(doc["embedding"]),
        key_embedding=list(doc["memory_key_embedding"]),
        key_text=str(head["memory_key"]) if int(doc.get("memory_key_embedding_dim") or 0) else "",
        excerpt=str(doc["content_excerpt"]),
        conflict_checked_at=doc.get("conflict_checked_at"),
    )


def _landed(
    group: list[_Entry],
    served: Mapping[int, Mapping[tuple[Any, ...], dict[str, Any]]],
    *,
    rows_in: bool,
    audits_in: bool,
    documents_in: bool,
) -> list[tuple[tuple[str, str, int], int]]:
    """Each delivery's targets that landed in this send: its memory and audit rows unless
    an earlier send landed them, and its search document where one was sent."""
    out: list[tuple[tuple[str, str, int], int]] = []
    for e in group:
        sent = served.get(id(e), {})
        for seq, row in zip(e.committed.seqs, e.committed.rows, strict=True):
            done = 0
            if not e.rows_sent:
                done |= (MEMORY_ROW if rows_in else 0) | (AUDIT_ROW if audits_in else 0)
            if documents_in and _doc_key(row) in sent:
                done |= SEARCH_DOC
            if done:
                out.append((seq, done))
    return out


async def _publish(group: list[_Entry]) -> None:
    """The group's memory rows, audit rows and search documents not yet sent, one insert
    per table (per block of partitions) under the earliest lease among them, then every
    delivered target cleared in one transaction, then the old vectors' retirements and
    deleted decisions' records the group owes. Raises what refused a memory row or a
    document, once what landed is cleared; what ClickHouse did not answer first."""
    ch = group[0].ch
    served = await _served_documents(group)
    # Leases checked again after that read: a commit whose lease ended during it is the
    # delivery worker's, not a reason to send the rest one by one.
    group = _within_lease(group)
    if not group:
        return
    deadline = min(e.committed.deadline for e in group)
    rows = [r for e in group if not e.rows_sent for r in e.committed.rows]
    docs = [d for e in group for d in served.get(id(e), {}).values()]

    async def send_rows() -> None:
        for block in partition_blocks(rows, created_month):
            await inserts.insert_memory_versions(block, ch=ch, published=True)

    async def send_documents() -> None:
        for block in partition_blocks(docs, created_month):
            await inserts.insert_embedding_rows(block, ch=ch)

    with pg_lease.leased(deadline):
        rows_error, audit_error, docs_error = await asyncio.gather(
            send_rows(),
            pg_publish.insert_audit_rows(
                [a for e in group if not e.rows_sent for a in e.committed.audits],
                ch=ch,
                retried=any(e.retried for e in group),
            ),
            send_documents(),
            return_exceptions=True,
        )
    if isinstance(audit_error, BaseException):
        log.warning(
            "audit rows not published yet; the delivery worker retries: %s",
            error_summary(audit_error),
        )
    rows_in = not isinstance(rows_error, BaseException)
    documents_in = not isinstance(docs_error, BaseException)
    await pg_memory.complete_each(
        _landed(
            group,
            served,
            rows_in=rows_in,
            audits_in=not isinstance(audit_error, BaseException),
            documents_in=documents_in,
        )
    )
    for e in group:
        e.rows_sent = e.rows_sent or rows_in
        e.documents_sent = e.documents_sent or documents_in
    failed = [x for x in (rows_error, docs_error) if isinstance(x, BaseException)]
    if failed:
        raise next((x for x in failed if _unanswered(x)), failed[0])
    await _publish_owed(group, ch=ch)


async def _publish_owed(group: list[_Entry], *, ch: _CHClient) -> None:
    """What published commits owe besides their rows, for all of them at once: the old
    vectors' retirement, then the deleted decisions' records (left owed when it fails,
    without a word). A retirement ClickHouse refused is tried again commit by commit, as a
    refused row is; an entry whose retirement fails is left to the delivery worker."""
    retiring = [e for e in group if any(t & pg_memory.OLD_VECTORS for t in e.committed.targets)]
    if retiring:
        try:
            await _retire(retiring, ch=ch)
        except Exception as e:  # noqa: BLE001 — the rows stand; the retirement stays owed
            await _each_alone(retiring, e, lambda entry: _retire([entry], ch=ch))
    records = [
        (e.committed, pg_publish.OwedRecord(row, seq, pg_memory.COMMIT_TOKEN))
        for e in group
        for row, seq in zip(e.committed.rows, e.committed.seqs, strict=True)
        if pg_memory.deleted_decision(row)
    ]
    if records:
        await pg_publish.retire_decision_records(
            [r for _, r in records], deadline=min(c.deadline for c, _ in records), ch=ch
        )


async def _retire(group: list[_Entry], *, ch: _CHClient) -> None:
    """The old vectors these entries' rows owe (`OLD_VECTORS`), retired in one call."""
    owing = [
        (seq, row, e.committed.deadline)
        for e in group
        for seq, row, targets in zip(
            e.committed.seqs, e.committed.rows, e.committed.targets, strict=True
        )
        if targets & pg_memory.OLD_VECTORS
    ]
    await pg_publish.retire_old_vectors(
        [row for _, row, _ in owing],
        [seq for seq, _, _ in owing],
        tokens=pg_memory.COMMIT_TOKEN,
        deadline=min(deadline for _, _, deadline in owing),
        ch=ch,
    )
