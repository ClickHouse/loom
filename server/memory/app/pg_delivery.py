"""Publication of committed revisions a request did not publish, under the Postgres
authority: by the delivery worker (`server.memory.workers.pg_delivery`), and by an
admission waiting on them (`pg_admission.wait_searchable`).

A claim publishes from the committed revision: the memory row, its audit row and the
search document (embedded here from the revision's text when ClickHouse lacks the
writer's). Claims go a namespace at a time, one by one when a batch fails, so a revision
that keeps failing holds back only itself. The attempts, stubs and parking rules are in
docs/architecture/storage-authority.md, "Delivery".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from server.core import ch_errors, pg, pg_lease
from server.core.error_summary import error_summary
from server.memory.store import pg_memory, pg_publish
from server.memory.store.pg_memory import Claimed
from server.memory.store.rows import identity_key, parse_memory_value, search_document_row
from server.settings import settings

log = logging.getLogger(__name__)


async def _search_documents(claims: list[Claimed], *, stub: bool = False) -> list[dict[str, Any]]:
    """The search documents of these claims, built from their revisions; `stub`: with
    no vector (`embedding_dim = 0`), for the embedding worker to fill."""
    from server.embedding import get_embedder
    from server.memory.domain.embed_text import embedding_texts

    if not claims:
        return []
    embedder = get_embedder()
    texts: list[tuple[str, str, str]] = []
    for c in claims:
        raw = str(c.row.get("value") or "")
        texts.append(
            embedding_texts(c.row["memory_type"], parse_memory_value(raw), raw, c.row["memory_key"])
        )
    vectors: dict[str, list[float]] = {}
    if not stub:
        wanted = sorted({t for _, embed, key in texts for t in (embed, key) if t})
        vectors = dict(zip(wanted, await embedder.embed_many(wanted), strict=True))
    # The writer's own document never landed, nor the resolver's mark it may have
    # carried: one rebuilt here owes the resolver a judgement whenever it runs.
    mark = None
    if settings.memory_conflict_resolver_enabled:
        from server.memory.store.conflict_resolver import OWED_MARK

        mark = OWED_MARK
    return [
        search_document_row(
            c.row,
            conflict_checked_at=mark,
            embedding_model=embedder.model_name,
            embedding=vectors.get(embed, []),
            key_embedding=vectors[key]
            if key in vectors
            else ([] if stub else [0.0] * embedder.dim),
            key_text=key,
            excerpt=excerpt,
        )
        for c, (excerpt, embed, key) in zip(claims, texts, strict=True)
    ]


async def publish_audit_outbox(limit: int) -> int:
    """Up to `limit` audit rows of the outbox (rows an erasure moved there, or those a
    claim deferred because the holder it replaced may still insert them) into ClickHouse,
    a namespace's claim at a time (`pg_memory.claim_outbox`), skipping a row already
    there. A refused batch is tried row by row: a row ClickHouse keeps refusing is
    counted alone, and parked at the attempts limit. ClickHouse unavailable
    (`ch_errors.refused_nothing`) or a claim that ran out refused nothing: the pass stops and the
    rows wait for their claim to end. Returns how many were published."""
    published = claimed = 0
    while claimed < limit:
        rows, start_by = await pg_memory.claim_outbox(limit - claimed)
        if not rows:
            break
        claimed += len(rows)
        error = await _publish_audits(rows, start_by)
        if error is None:
            published += len(rows)
            continue
        if await _stops_pass(error):
            break
        if len(rows) == 1:
            await _refused(rows, error)
            continue
        for row in rows:
            error = await _publish_audits([row], start_by)
            if error is None:
                published += 1
            elif await _stops_pass(error):
                return published
            else:
                await _refused([row], error)
    return published


async def _stops_pass(error: Exception) -> bool:
    return isinstance(error, pg_publish.ClaimExpired) or await ch_errors.refused_nothing(
        error, probe=True
    )


async def _refused(rows: list[dict[str, Any]], error: Exception) -> None:
    log.warning("pg_delivery: audit outbox row refused: %s", error_summary(error))
    await pg_memory.fail_outbox(rows, error)


async def _publish_audits(rows: list[dict[str, Any]], start_by: float) -> Exception | None:
    """Insert these audit rows, skipping any already in ClickHouse, and drop them from
    the outbox; returns the error that refused them."""
    from server.core.ch import get_client

    try:
        await pg_publish.insert_audit_rows(rows, ch=get_client(), retried=True, start_by=start_by)
    except Exception as e:  # noqa: BLE001 — the caller counts the refusal on the rows
        return e
    await pg_memory.drop_outbox(rows)
    return None


async def _publish(claims: list[Claimed], *, stub: bool = False) -> None:
    """One namespace's claims: memory rows and audit rows (`pg_publish.publish_rows`),
    then the search documents still owed, the old vectors' retirements, then the deleted
    decisions' records."""
    import server.memory.store as memory_repo
    from server.core.ch import get_client
    from server.memory.app import after_write

    ch = get_client()
    deadline = min(c.deadline for c in claims)
    # A holder replaced on a lease that ended within the insert bound may still land its
    # audit row, which a read now would miss: it goes through the outbox, due once it cannot.
    bound = pg.insert_bound_s()
    deferred = [
        c for c in claims if c.targets & pg_memory.AUDIT_ROW and c.replaced_lease_s > -bound
    ]
    if deferred:
        await pg_memory.defer_audits(deferred, bound)
    sent_later = {_key(c) for c in deferred}
    refused = await pg_publish.publish_rows(
        [c.row for c in claims if c.targets & pg_memory.MEMORY_ROW],
        [c.audit for c in claims if c.targets & pg_memory.AUDIT_ROW and _key(c) not in sent_later],
        [(c.org, c.namespace, c.seq) for c in claims],
        tokens=[c.lease_token for c in claims],
        deadline=deadline,
        retried=True,
        ch=ch,
    )
    owed = [c for c in claims if c.targets & pg_memory.SEARCH_DOC]
    held = await pg_publish.held_documents(
        claims[0].org, claims[0].namespace, [pg_publish.document_id(c.row) for c in owed], ch=ch
    )
    build = await _still_heads(
        [c for c in owed if pg_publish.document_id(c.row) not in held], deadline
    )
    docs = await _search_documents(build, stub=stub)
    if docs:
        with pg_lease.leased(deadline):
            await memory_repo.insert_embedding_rows(docs, ch=ch)
        after_write.index_late_rows(docs, ch=ch)
    await pg_memory.complete(
        [(c.org, c.namespace, c.seq) for c in owed],
        pg_memory.SEARCH_DOC,
        tokens=[c.lease_token for c in owed],
    )
    old_vectors = await _retire_old_vectors(
        [c for c in claims if c.targets & pg_memory.OLD_VECTORS], deadline, ch=ch
    )
    unretired = await pg_publish.retire_decision_records(
        [
            pg_publish.OwedRecord(c.row, (c.org, c.namespace, c.seq), c.lease_token)
            for c in claims
            if c.targets & pg_memory.DECISION_RECORD
        ],
        deadline=deadline,
        ch=ch,
    )
    failed = refused or old_vectors or unretired
    if failed is not None:
        raise failed  # an attempt that failed, once the rest is in


async def _still_heads(claims: list[Claimed], deadline: float) -> list[Claimed]:
    """The claims whose document the key's head still serves, each with the row to build
    it from: its own revision while that is the head, else a newer copy of it (a pin, a
    touch, a status) that owes no document of its own, whose row the document carries.
    A redacted head serves none. A newer head that is deleted, not embedded or of other
    content has its own document, or its retirement of the vectors below it, which one
    built for the older revision now would land live above.

    A copy is another commit than the claim's, perhaps one on the primary alone: the
    heads are read again durably (`pg_memory.heads_by_keys`) and the documents built
    from that read; a standby wait that ended the lease is an outage (`pg_lease.check_room`)."""
    if not claims:
        return claims
    keys = [identity_key(c.row) for c in claims]
    heads = await pg_memory.heads_by_keys(keys, include_tombstoned=True)
    if any(_newer_copy(c, heads) for c in claims):
        pg_lease.check_lease(deadline)  # a lease ClickHouse spent counts, as before
        heads = await pg_memory.heads_by_keys(keys, include_tombstoned=True, durable=True)
        pg_lease.check_room(deadline)
    return [replace(c, row=row) for c in claims if (row := _served(c, heads)) is not None]


def _newer_copy(c: Claimed, heads: dict[Any, dict[str, Any]]) -> bool:
    """Whether `c`'s key's head is a newer copy of its revision (`pg_memory.newer_copy`)."""
    head = heads.get(identity_key(c.row))
    return head is not None and pg_memory.newer_copy(head, c.row)


def _served(c: Claimed, heads: dict[Any, dict[str, Any]]) -> dict[str, Any] | None:
    """The row `c`'s document is built from (`_still_heads`), None for none."""
    head = heads.get(identity_key(c.row))
    if head is None or head.get("redacted_at") is not None:
        return None
    if int(head["version"]) <= int(c.row["version"]):
        return c.row
    return head if pg_memory.newer_copy(head, c.row) else None


async def _retire_old_vectors(
    claims: list[Claimed], deadline: float, *, ch: Any
) -> Exception | None:
    """Retire the vectors below each claim's revision, which is not embedded, and clear
    its OLD_VECTORS; the error, for the worker to count once the rest is in."""
    if not claims:
        return None
    try:
        await pg_publish.retire_old_vectors(
            [c.row for c in claims],
            [_key(c) for c in claims],
            tokens=[c.lease_token for c in claims],
            deadline=deadline,
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — owed still; the rest of the claims go on
        log.warning("old vectors' retirement still owed: %s", error_summary(e))
        return e
    return None


class _Stopped(Exception):
    """The pass ends with nothing refused (ClickHouse unavailable, or the worker's time
    for it is up): its claims are released."""


def _key(c: Claimed) -> tuple[str, str, int]:
    return (c.org, c.namespace, c.seq)


@dataclass
class _Pass:
    """One delivery pass. The worker's (`worker`) probes ClickHouse before a failed
    connection counts, and when it is cancelled keeps the mark of the publish that was
    running (`running`, by delivery), so one that always outlasts the pass counts. An
    admission helping does neither: what the worker would not count, it does not."""

    worker: bool
    running: set[tuple[str, str, int]] = field(default_factory=set)
    landed: int = 0  # revisions published
    failed: int = 0  # failed attempts counted
    # The worker stops starting publishes a lease into its pass, well inside the pass
    # deadline: a cut at that deadline lands on a publish slow on its own, not on one
    # that only queued behind others. What it did not start is released, uncounted.
    stop_at: float = field(
        default_factory=lambda: time.monotonic() + settings.pg_delivery_lease_seconds
    )

    def check_time(self) -> None:
        """Raise `_Stopped` once the worker's time for the pass is up."""
        if self.worker and time.monotonic() > self.stop_at:
            log.info("pg_delivery: the pass's time is up; the rest wait for the next one")
            raise _Stopped


async def publish_claims(claims: list[Claimed], *, worker: bool = True) -> tuple[int, int]:
    """Publish claimed deliveries, a namespace at a time; how many landed, and how many
    failed attempts were counted. A namespace whose batch fails is retried claim by
    claim, and so is a claim taken over from a publish that stopped the process; a
    claim that fails on its last attempt, owing its search document and not its memory
    row, publishes a stub of it. A pass that ends without an outcome for a claim
    (ClickHouse unavailable, a Postgres outage, a cancellation) releases it: taken over
    again, it counts no attempt (`pg_memory.release`); the worker's cancelled pass keeps
    the mark of the publish that was running (`_Pass`)."""
    run = _Pass(worker=worker)
    # A namespace at a time, the one holding the oldest claim first (`claim_due`'s order).
    groups: dict[tuple[str, str], list[Claimed]] = {}
    for c in claims:
        groups.setdefault(_ns(c), []).append(c)
    try:
        for group in groups.values():
            run.check_time()
            await _publish_namespace(group, run)
    except _Stopped:
        await pg_memory.release(claims)
    except asyncio.CancelledError:
        # The publish running when the pass was cut keeps its mark: one that always
        # outlasts the pass counts and parks like any other. Shielded, so the rest are
        # forgotten however the cancellation lands.
        await asyncio.shield(pg_memory.release([c for c in claims if _key(c) not in run.running]))
        raise
    except BaseException:
        await asyncio.shield(pg_memory.release(claims))
        raise
    return run.landed, run.failed


async def _publish_namespace(group: list[Claimed], run: _Pass) -> None:
    """One namespace's claims, together, then one by one when that fails, counted on
    `run` as they go. A Postgres outage is no outcome: the pass ends and releases its
    claims. A refused statement fails the same way on a retry, so it is the claim's
    attempt, counted and parked like a refusal by ClickHouse."""
    limit = settings.pg_delivery_max_attempts
    # Past the limit only a stub is published, of a claim owing only its search document.
    spent = [c for c in group if c.attempts >= limit]
    for c in spent:
        await _last_attempt(c, None, run)
        run.check_time()  # before the next stub or this namespace's batch
    alone = [c for c in group if c.taken_over and c.attempts < limit]
    # A full lease for this namespace's turn, however long the ones before it took.
    batch = await pg_memory.renew([c for c in group if not c.taken_over and c.attempts < limit])
    if batch:
        try:
            await _publish_marked(batch, run)
            run.landed += len(batch)
            batch = []
        except pg.PostgresUnavailableError:
            raise
        except Exception as e:  # each claim is tried on its own below
            why = error_summary(e)
            if await ch_errors.refused_nothing(e, probe=run.worker):
                log.warning("pg_delivery: ClickHouse unavailable (%s); pass stopped", why)
                raise _Stopped from e
            log.warning("pg_delivery: a batch of %s failed (%s); one at a time", _ns(batch[0]), why)
    async for c in _one_by_one([*alone, *batch], run):
        try:
            await _publish_marked([c], run)
            run.landed += 1
        except pg.PostgresUnavailableError:
            raise
        except Exception as e:  # one revision must not hold back the rest
            if await ch_errors.refused_nothing(e, probe=run.worker):
                raise _Stopped from e
            # A publish its lease ran out on (PublicationPending) counts too: one that
            # always outlasts it would otherwise be claimed again on every pass.
            if c.attempts + 1 >= limit:  # this one counts, and is the last
                await _last_attempt(c, e, run)
            else:
                run.failed += await pg_memory.record_failure([c], e)


async def _publish_marked(claims: list[Claimed], run: _Pass, *, stub: bool = False) -> None:
    """`_publish`, with the claims marked as running while it does: a worker's pass
    cancelled in it keeps them held. A publish that ends, either way, clears the mark."""
    keys = {_key(c) for c in claims} if run.worker else set()
    run.running |= keys
    try:
        await _publish(claims, stub=stub)
    except asyncio.CancelledError:
        raise
    except BaseException:
        run.running -= keys
        raise
    run.running -= keys


async def _last_attempt(c: Claimed, error: Exception | None, run: _Pass) -> None:
    """A claim whose publish failed on its last attempt (`error`), or that comes back
    past the limit (None): a stub of its search document when that is all it owes,
    otherwise parked; counted on `run`."""
    try:
        if await _publish_stub(c, run):
            run.landed += 1
            return
    except pg.PostgresUnavailableError:
        raise
    except Exception as e:  # the stub's refusal is the attempt's
        if await ch_errors.refused_nothing(e, probe=run.worker):
            raise _Stopped from e
        error = e
    if error is None:
        return
    counted = await pg_memory.record_failure([c], error, park=True)
    run.failed += counted
    if counted:
        log.error("pg_delivery: %s seq %s parked: %s", _ns(c), c.seq, error_summary(error))


async def _one_by_one(batch: list[Claimed], run: _Pass) -> AsyncIterator[Claimed]:
    """The batch's claims, each with a full lease again just before its own publish,
    until the pass's time is up (`_Pass.check_time`): what is left is not renewed."""
    for c in batch:
        run.check_time()
        for renewed in await pg_memory.renew([c]):
            yield renewed


async def _publish_stub(c: Claimed, run: _Pass) -> bool:
    """A stub of the claim's search document, under a full lease again, when its memory
    row is in and its search document owed: it is findable until it is embedded. False
    when the claim is no longer held or owes its memory row; raises what refused the
    stub or the audit row it still owes."""
    renewed = await pg_memory.renew([c])
    if not renewed:
        return False
    held = renewed[0]
    if held.targets & pg_memory.SEARCHED != pg_memory.SEARCH_DOC:
        await pg_memory.release(renewed)
        return False
    await _publish_marked([held], run, stub=True)
    return True


def _ns(c: Claimed) -> tuple[str, str]:
    return (c.org, c.namespace)


async def deliver_namespace(org: str, namespace: str) -> int:
    """Publish a few of the namespace's deliveries whose lease ran out, now
    (LOOM_PG_ADMISSION_HELP_ROWS, oldest first): what an admission waiting on them does
    instead of waiting for the next pass. Never raises: another writer's revision that
    fails is the delivery worker's to retry, and the admission keeps waiting."""
    try:
        claims = await pg_memory.claim_due(settings.pg_admission_help_rows, scope=(org, namespace))
        landed, _ = await publish_claims(claims, worker=False) if claims else (0, 0)
        return landed
    except Exception as e:  # noqa: BLE001 — someone else's delivery must not fail this write
        log.warning("pg_delivery: help for %s/%s failed: %s", org, namespace, error_summary(e))
        return 0
