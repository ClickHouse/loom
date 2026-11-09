"""Status and tombstone writes over loom.memory that carry the change onto
the embeddings mirror: version rows by id, supersession and its inverse,
consolidated dependents of a deleted source, scope clears and by-id
tombstone cascades.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from server.core import pg, pg_lease
from server.core.ch import sequential_reads
from server.core.datetimes import naive_utc_now, naive_utc_now_seconds
from server.core.error_summary import error_summary
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.lock import assert_unlocked
from server.memory.domain.redaction_lineage import is_content_hash
from server.memory.domain.webhook_events import memory_change
from server.memory.store import pg_memory, pg_publish
from server.memory.store.embeddings import (
    mirror_status_to_embeddings,
    reemit_embedding_rows_for_keys,
    reject_embedding_rows,
    retire_vectors_for_keys,
)
from server.memory.store.inserts import (
    check_memory_rows,
    insert_embedding_rows,
    insert_memory_versions,
)
from server.memory.store.reads import get_by_id, memory_rows_by_identity_keys
from server.memory.store.records import tombstone_decision_records
from server.memory.store.redaction import delete_by_key
from server.memory.store.rows import (
    IdentityKey,
    MemoryRow,
    _hydrate,
    build_memory_version_row,
    identity_key,
    key_in_namespace,
    next_version,
    row_field,
    version_over,
)
from server.memory.store.sql import _SEQUENTIAL
from server.observability import degradations
from server.settings import settings

_log = logging.getLogger(__name__)

# Rows `write_versions_by_ids` reads concurrently.
_READ_AT_ONCE = 16  # not a setting: the fan-out of one call


async def write_versions_by_ids(
    org: str,
    memory_ids: list[str],
    update: Callable[[MemoryRow], dict[str, Any]],
    *,
    namespace: str,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
    strong: bool = False,
    on_written: Callable[[list[dict[str, Any]]], None] | None = None,
    on_unconfirmed: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    judged: bool = False,
    strict: bool = False,
) -> int:
    """Write a new version of each live row in `memory_ids`: `update(existing)`'s
    overrides on every persisted column carried forward, a fresh `version` unless it
    sets one; an `update` that raises writes nothing, since every row is built before
    any is written; `skip` and `update` run once every row is read. `namespace`: the
    rows of that namespace (`reads.get_by_id`). `skip` leaves a row as it is; `strong`
    reads read-your-writes, the status mirror's vector rows included.
    `on_written` / `on_unconfirmed` hear what the re-read confirmed or could not (see
    Lifecycle in memory-architecture.md). `judged`: the rows land a verdict reached
    after the write (the conflict resolver's) on the write it judged
    (`conflicts.judged_write`), and the status mirror copies only that write's vector
    rows (`mirror_status_to_embeddings(judged=True)`). `strict`: a failed status
    mirror raises instead of being logged.
    Returns the rows written, or confirmed."""

    if not memory_ids:
        return 0
    ch = ch or _default_client()
    found = await _heads_by_ids(org, namespace, memory_ids, ch=ch, strong=strong)
    rows: list[dict[str, Any]] = []
    sources: list[MemoryRow] = []
    for existing in found:
        if existing is None or (skip is not None and skip(existing)):
            continue
        overrides = dict(update(existing))
        if "version" not in overrides:
            overrides["version"] = version_over(existing)
        rows.append(build_memory_version_row(existing, **overrides))
        sources.append(existing)

    if not rows:
        return 0
    await insert_versions_over_heads(rows, sources, ch=ch)
    confirmed = rows
    if on_written is not None:
        # Under Postgres the commit is over each head read (`heads_unchanged(exact=True)`)
        # and keeps the newer version: a row is the head unless it is older than its read.
        heads = (
            {
                _key_in_org(r): max(int(r["version"]), s.version)
                for r, s in zip(rows, sources, strict=True)
            }
            if pg.enabled()
            else await _head_versions(org, rows, ch=ch)
        )
        confirmed = [r for r in rows if heads.get(_key_in_org(r)) == int(r["version"])]
        on_written(confirmed)
        # Not seen at the head yet (no key, or an older version): it may or may not land.
        unconfirmed = [r for r in rows if heads.get(_key_in_org(r), -1) < int(r["version"])]
        if unconfirmed and on_unconfirmed is not None:
            await on_unconfirmed(unconfirmed)
        # Mirrored: what was confirmed, or, with no one to reconcile the rest, all but
        # the overtaken.
        rows = (
            confirmed
            if on_unconfirmed is not None
            else [r for r in rows if heads.get(_key_in_org(r), -1) <= int(r["version"])]
        )
    await _mirror_statuses(rows, ch=ch, judged=judged, strict=strict, sequential=strong)
    return len(confirmed)


async def _heads_by_ids(
    org: str, namespace: str, memory_ids: list[str], *, ch: _CHClient, strong: bool
) -> list[MemoryRow | None]:
    """Each id's head in `namespace`, once each in the order given, at most
    `_READ_AT_ONCE` reads in flight."""
    ids = list(dict.fromkeys(mid for mid in map(str, memory_ids) if mid))
    gate = asyncio.Semaphore(_READ_AT_ONCE)

    async def read(mid: str) -> MemoryRow | None:
        async with gate:
            return await get_by_id(
                mid, org=org, namespace=namespace, ch=ch, scope=WHOLE_NAMESPACE, strong=strong
            )

    reads = [asyncio.ensure_future(read(mid)) for mid in ids]
    try:
        return list(await asyncio.gather(*reads))
    except BaseException:
        # Stop the other reads, letting any already cancelled finish their cleanup.
        for r in reads:
            if not r.cancelling():
                r.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        raise


async def _mirror_statuses(
    rows: list[dict[str, Any]], *, ch: Any, judged: bool, strict: bool, sequential: bool
) -> None:
    """A status change on loom.memory mirrored onto the row memory.search reads: the pool
    filters and penalises on memory_embeddings.status, so without it search would keep
    admitting superseded and unpenalised conflicted rows."""
    status_changes = [r for r in rows if r.get("status") and r.get("status") != "active"]
    if status_changes:
        await mirror_status_to_embeddings(
            status_changes, ch=ch, judged=judged, strict=strict, sequential=sequential
        )


async def authority_heads(rows: list[MemoryRow]) -> list[MemoryRow | None]:
    """Each row's live head as the Postgres authority holds it now (None when there is
    none): a row read from ClickHouse may trail it by its delivery. On ClickHouse, the
    rows as they are."""
    if not pg.enabled() or not rows:
        return list(rows)
    keys = [identity_key(r) for r in rows]
    heads = await pg_memory.heads_by_keys(keys)
    return [_hydrate(heads[k]) if k in heads else None for k in keys]


async def served_rows[R](
    rows: list[R], still: Callable[[R, dict[str, Any]], bool] | None = None
) -> list[R]:
    """The live rows read from ClickHouse (MemoryRows or row dicts) that the Postgres
    authority still serves: each its key's live head at the version read, or at another
    version `still(row, head)` keeps, with the same redaction state either way, so a
    delete, a rewrite or a redaction ClickHouse has not published is not read back. On
    ClickHouse, the rows."""
    if not pg.enabled() or not rows:
        return rows
    keys = [identity_key(r) for r in rows]
    heads = await pg_memory.heads_by_keys(keys)
    return [
        r
        for r, k in zip(rows, keys, strict=True)
        if (h := heads.get(k)) is not None
        and (h.get("redacted_at") is None) == (row_field(r, "redacted_at") is None)
        and (
            int(h["version"]) == int(row_field(r, "version")) or (still is not None and still(r, h))
        )
    ]


# The columns a recovery listing reads and filters on, besides the delete itself and the
# trace (a delete rewrites it; `recovered_rows` checks a trace filter on its own): a deleted
# head that moved on in any of them since ClickHouse's row was read is not what the
# listing selected.
_LISTED_AS_READ = (
    "memory_id",
    "memory_type",
    "content_sha256",
    "tags",
    "valid_at",
    "created_at",
    "sensitivity",
    "pinned",
    "run_id",
)


async def recovered_rows(
    rows: list[MemoryRow],
    *,
    source_trace: str | None = None,
    hidden_statuses: frozenset[str] = frozenset(),
    as_of: datetime | None = None,
) -> list[MemoryRow]:
    """A recovery listing's rows (deleted ones included) as the Postgres authority holds
    them: a row at its head's version stands; one the authority has since only deleted is
    listed as that deleted head with ClickHouse's preview of the text; any other
    (rewritten, relabelled, redacted, restored, gone) leaves the page until ClickHouse
    holds it, so no text ClickHouse has not published is read back and the listing's
    filters hold (`source_trace`, `hidden_statuses`; under `as_of`, which reads
    `temporal_anchor`, a column the listing does not select, no head is swapped in). On
    ClickHouse, the rows."""
    if not pg.enabled() or not rows:
        return rows
    keys = [identity_key(r) for r in rows]
    heads = await pg_memory.heads_by_keys(keys, include_tombstoned=True)
    listed: list[MemoryRow] = []
    for r, k in zip(rows, keys, strict=True):
        stored = heads.get(k)
        if stored is None:
            continue
        head = _hydrate(stored)
        if (head.redacted_at is None) != (r.redacted_at is None):
            continue
        if head.version == r.version:
            listed.append(r)
        elif (
            head.tombstoned_at is not None
            and r.tombstoned_at is None
            and all(getattr(head, c) == getattr(r, c) for c in _LISTED_AS_READ)
            and source_trace in (None, head.source_trace)
            and head.status not in hidden_statuses
            and as_of is None
        ):
            preview = {
                "value": r.value,
                "value_text": r.value_text,
                "value_truncated": r.value_truncated,
            }
            listed.append(head.model_copy(update=preview))
    return listed


async def current_heads(
    rows: list[MemoryRow], still: Callable[[MemoryRow, MemoryRow], bool] | None = None
) -> list[MemoryRow]:
    """The authority's head of each of ClickHouse's `rows` a copy may be built from: the
    version read, or a newer one that `still(row, head)` keeps because it still
    qualifies, so a key whose delivery is parked is not left behind on every pass. On
    ClickHouse, the rows."""
    heads = await authority_heads(rows)
    return [
        h
        for r, h in zip(rows, heads, strict=True)
        if h is not None and (h.version == r.version or (still is not None and still(r, h)))
    ]


async def insert_versions_over_heads(
    rows: list[dict[str, Any]],
    read: Sequence[MemoryRow | None],
    *,
    ch: _CHClient | None = None,
    then: Callable[[], Awaitable[int | None]] | None = None,
    landed: Callable[[], Awaitable[None]] | None = None,
    documented: Sequence[bool] | None = None,
    settle_held: bool = False,
    sources: Sequence[tuple[str, int]] = (),
    live_sources: bool = False,
    on_commit: Callable[[], None] | None = None,
) -> int:
    """Write `rows`, each a copy of the authority's head beside it in `read`, drawn above
    it (`rows.version_over`) or, to yield to a write since, before the read: a copy below
    the head is overtaken by it. Under Postgres only over that very version, else 503
    (`heads_unchanged(exact=True)`): a copy never overwrites a write, a relabel or a
    redaction it did not see; a key with no head read (None) is held to having no live
    head. On ClickHouse, the plain insert.

    `then` runs once the rows are in and its count is returned; under Postgres within the
    commit's lease, so a redaction waits out the ClickHouse writes it makes, and a failure
    of it is raised once the commit is published. `landed` runs once the rows are in,
    outside the lease: on ClickHouse right after the insert, before `then`; under Postgres
    after the publish, whatever `then` did. `documented`: the rows that owe a live search
    document, which `then` writes; under Postgres they are settled once `then` returns
    (with `settle_held`, only those ClickHouse holds at their row's version, for a `then`
    that copies what it finds), and the delivery worker builds one it did not.
    `sources`: the revisions the rows copy (`Admission.sources`), with `live_sources`
    each still its key's live head. `on_commit` is called once the rows are in: under
    Postgres as the commit returns, before anything else can fail. A commit whose
    publication is pending is done."""
    if not pg.enabled() or not rows:
        if rows:
            await insert_memory_versions(rows, ch=ch)
            if on_commit is not None:
                on_commit()
            if landed is not None:
                await landed()
        return 0 if then is None else (await then() or 0)
    check_memory_rows(rows)
    heads = {identity_key(r): e for r, e in zip(rows, read, strict=True)}
    admission = pg_memory.Admission(
        recheck=pg_memory.heads_unchanged(heads, exact=True),
        sources=sources,
        live_sources=live_sources,
    )
    plain = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
    owed = [plain | (pg_memory.SEARCH_DOC if d else 0) for d in documented or [False] * len(rows)]
    try:
        committed = await pg_publish.commit(rows, targets=owed, admission=admission)
    except pg_memory.HeadMoved:
        raise pg_memory.AdmissionContendedError("the memory changed while it was read") from None
    if on_commit is not None:
        on_commit()
    answer, failed = 0, None
    if then is not None:
        try:
            with pg_lease.leased(committed.deadline), sequential_reads():
                answer = await then() or 0
            if documented:
                await _settle_documents(committed, documented, held=settle_held, ch=ch)
        except pg_lease.PublicationPending:
            _log.warning("a copy's follow-up was left: its commit's lease ended first")
        except Exception as e:  # noqa: BLE001 — raised once the commit is published
            failed = e
    try:
        await pg_publish.publish(committed, ch=ch)
    except pg_lease.PublicationPending:
        _log.info("committed %d version row(s) left for the delivery worker", len(rows))
    if landed is not None:
        await landed()
    if failed is not None:
        raise failed
    return answer


async def insert_documented_versions(
    rows: list[dict[str, Any]], documents: list[dict[str, Any]], *, ch: _CHClient | None = None
) -> bool:
    """Write `rows` with their search documents (`documents`, one embedding row for each),
    rechecking no head. Whether the rows reached ClickHouse: False when their publication
    is pending, which is done. Under Postgres the commit owes the documents and they are
    published with the rows, within the commit's lease, so an erasure waits them out and
    the delivery worker builds one a failure left; a refused document leaves the
    publication pending, so False, though the rows may be in. On ClickHouse the rows,
    then their documents; a failed document is logged, not raised."""
    if len(documents) != len(rows):
        raise ValueError("one search document per row")
    if not pg.enabled():
        await insert_memory_versions(rows, ch=ch)
        try:
            await insert_embedding_rows(documents, ch=ch)
        except Exception as e:  # noqa: BLE001 — the rows stand; the embedding worker stubs one
            _log.warning("search documents not written: %s", error_summary(e))
        return True
    check_memory_rows(rows)
    owed = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC | pg_memory.AUDIT_ROW
    committed = await pg_publish.commit(rows, targets=owed)
    try:
        await pg_publish.publish(committed, documents=documents, ch=ch)
    except pg_lease.PublicationPending:
        _log.info("committed %d version row(s) left for the delivery worker", len(rows))
        return False
    return True


async def _settle_documents(
    committed: Any, documented: Sequence[bool], *, held: bool = False, ch: _CHClient | None = None
) -> None:
    """Clear the search documents written under the commit's lease; with `held`, only
    those that ClickHouse holds at their row's version (`held_documents`). Failing that,
    the delivery worker settles one it finds there and builds again one written below it
    (an Undo's rows, re-emitted at their own)."""
    owed = [(r, q) for r, q, d in zip(committed.rows, committed.seqs, documented, strict=True) if d]
    try:
        if held:
            client = ch or _default_client()
            by_scope: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
            for r, _ in owed:
                by_scope.setdefault((r["org"], r["namespace"]), []).append(
                    pg_publish.document_id(r)
                )
            held_ids: set[tuple[Any, ...]] = set()
            for (org, ns), ids in by_scope.items():
                docs = await pg_publish.held_documents(org, ns, ids, ch=client)
                held_ids |= {(org, ns, *d) for d in docs}
            owed = [
                (r, q)
                for r, q in owed
                if (r["org"], r["namespace"], *pg_publish.document_id(r)) in held_ids
            ]
        await pg_memory.complete([q for _, q in owed], pg_memory.SEARCH_DOC)
    except Exception as e:  # noqa: BLE001 — the commit stands; the worker settles or builds them
        _log.warning("documents left owed for the delivery worker: %s", error_summary(e))


async def _head_versions(
    org: str, rows: list[dict[str, Any]], *, ch: _CHClient
) -> dict[tuple[str, ...], int]:
    """The head version at each of `rows`' keys under FINAL, read up to twice;
    empty when both reads fail, so nothing is confirmed."""
    keys = sorted({_key_in_org(r) for r in rows})
    for attempt in (1, 2):
        try:
            heads = await ch.query_all(
                """
                SELECT namespace, agent, user_id, session_id, memory_key, version
                FROM loom.memory FINAL
                WHERE org = {org:String}
                  AND (namespace, agent, user_id, session_id, memory_key)
                      IN {keys:Array(Tuple(String, String, String, String, String))}
                SETTINGS select_sequential_consistency = 1
                """,
                params={"org": org, "keys": keys},
            )
            return {_key_in_org(h): int(h["version"]) for h in heads}
        except Exception as e:  # noqa: BLE001 — the mirror must still run for what was written
            _log.warning("write_versions_by_ids: head re-read %d failed: %s", attempt, e)
            if attempt == 1:
                await asyncio.sleep(0.05)
    return {}


def _key_in_org(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (str(row["namespace"]), *key_in_namespace(row))


async def supersede_duplicates(
    org: str,
    victims: list[dict[str, Any]],
    *,
    namespace: str,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
    strong: bool = False,
    on_written: Callable[[list[dict[str, Any]]], None] | None = None,
    on_unconfirmed: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    judged: bool = False,
) -> int:
    """Mark near-duplicate memories status='superseded' in BOTH loom.memory
    and loom.memory_embeddings via version re-emit (never an in-place ALTER,
    which diverges under FINAL).

    `memory.search` reads loom.memory_embeddings and keeps only status IN
    ('active','duplicate','conflicted'), so the EMBEDDINGS write is what
    removes the row from recall; the loom.memory write keeps the audit trail
    and the `superseded_by` back-pointer consistent (mirror of
    conflicts_repo.tombstone_ids).

    `victims`: dicts carrying `memory_id`, the embedding identity
    (`agent`, `user_id`, `session_id`, `memory_key`) and `superseded_by`
    (the kept row's memory_id). Empty short-circuits. A victim whose head
    row `skip` answers True for (the caller's policy, e.g. a lock) is left
    live in both tables and not counted. `strong` reads the rows read-your-writes
    (`write_versions_by_ids`). `on_written` hears the rows whose retirement the
    re-read confirmed, `on_unconfirmed` those it could not tell about
    (`write_versions_by_ids`). `judged` (a verdict reached after the write): each
    row is superseded at its read version + 1, so a write drawn after that read stays
    newer. Returns how many were written; with `on_written`, how many were confirmed."""
    if not victims:
        return 0
    ch = ch or _default_client()

    # 1) loom.memory — status + superseded_by, carrying every column forward.
    retired_at = naive_utc_now()
    version = next_version()  # the supersede's, which the vector retire carries
    by_id = {str(v["memory_id"]): str(v.get("superseded_by") or "") for v in victims}
    # `write_versions_by_ids` also mirrors the flip onto loom.memory_embeddings, the
    # table recall reads, at the supersede's version.
    return await write_versions_by_ids(
        org,
        list(by_id),
        lambda row: {
            "status": "superseded",
            "superseded_by": by_id.get(str(row.memory_id), ""),
            # record-axis retirement stamp: knowledge_time replay before this
            # instant still sees the row, after it does not
            "invalid_at": retired_at,
            "version": int(row.version) + 1 if judged else version,
        },
        namespace=namespace,
        ch=ch,
        skip=skip,
        strong=strong,
        on_written=on_written,
        on_unconfirmed=on_unconfirmed,
        judged=judged,
    )


async def reject_by_identity(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    memory_key: str,
    reason: str,
    *,
    skip: Callable[[MemoryRow], bool] | None = None,
    ch: _CHClient | None = None,
) -> list[str]:
    """Mark the live rows of `memory_key` in every session of the identity
    `rejected`. A locked row raises 423 LOCKED before any row is written, unless
    `skip` leaves it. Rows are marked by version re-emit on loom.memory and its
    vector rows. Never a lightweight UPDATE: on loom.memory_embeddings it leaves a
    patch part, and the fact leg's codes-first read stays off while one exists
    (`server.core.vectors.patch_parts`). Each row lands at a version drawn before
    the read, lifted above its head when that is newer (`version_over`): every head
    the read found is rejected, a write drawing a version above a row's outranks it, and
    the rows sit above any vector row an earlier metadata copy left at read + 1
    (`reemit_version`).
    A failed vector write raises: recall reads the vector row. Returns the ids of the
    rows written. Under Postgres, `_reject_heads`."""
    ch = ch or _default_client()
    if pg.enabled():
        return await _reject_heads(
            org, namespace, agent, user_id, memory_key, reason, skip=skip, ch=ch
        )
    version = next_version()
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND memory_key = {key:String} AND tombstoned_at IS NULL
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "agent": agent, "uid": user_id, "key": memory_key},
    )

    written: list[str] = []

    def reject(row: MemoryRow) -> dict[str, Any]:
        assert_unlocked(row, "delete", op="memory.reject")
        written.append(str(row.memory_id))  # every row built is written, or none is
        return {
            "status": "rejected",
            "rejection_reason": reason,
            "version": version_over(row, version),
        }

    await write_versions_by_ids(
        org,
        [str(r["memory_id"]) for r in rows],
        reject,
        ch=ch,
        skip=skip,
        namespace=namespace,
        strong=True,
        strict=True,
    )
    return written


async def _reject_heads(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    memory_key: str,
    reason: str,
    *,
    skip: Callable[[MemoryRow], bool] | None = None,
    ch: Any,
) -> list[str]:
    """`reject_by_identity` under the Postgres authority: each head takes the status as a
    revision committed only over the version read, and its vectors move to that version
    under the commit's lease (`_reject_vectors`), so an older search document still owed
    lands under them; a head written since is read again, and `skip` and the lock judge
    it again."""
    for _ in range(settings.pg_admission_rounds):
        heads = [
            h
            for h in map(
                _hydrate, await pg_memory.live_heads_of(org, namespace, agent, user_id, memory_key)
            )
            if skip is None or not skip(h)
        ]
        for h in heads:
            assert_unlocked(h, "delete", op="memory.reject")
        rows = [
            build_memory_version_row(
                h, status="rejected", rejection_reason=reason, version=version_over(h)
            )
            for h in heads
        ]
        if not rows:
            return []
        try:
            await insert_versions_over_heads(
                rows, heads, ch=ch, then=functools.partial(_reject_vectors, rows, ch=ch)
            )
            return [str(h.memory_id) for h in heads]  # the pass that committed
        except pg_memory.AdmissionContendedError:
            continue  # a head moved since the read: read them again
    raise pg_memory.AdmissionContendedError("the key's heads kept changing")


async def _reject_vectors(rows: list[dict[str, Any]], *, ch: Any) -> int:
    """Copy each rejected head's own vectors at its version as 'rejected' and retire every
    other vector below that version with no text (`reject_embedding_rows`); a redacted,
    unhashed or unembedded head keeps none."""
    items = []
    for r in rows:
        sha = str(r.get("content_sha256") or "")
        keeps_vectors = is_content_hash(sha) and pg_memory.embedded(r)
        items.append((identity_key(r), int(r["version"]), sha if keeps_vectors else None))
    return await reject_embedding_rows(items, ch=ch)


async def consolidated_dependents_of(
    org: str, namespace: str, source_key: str, *, ch: _CHClient | None = None
) -> dict[IdentityKey, frozenset[str]]:
    """The live summaries built FROM `source_key`, rows tagged `consolidated` and
    `src:<key>` by their writer, as the authority holds them: each key with the sources
    its tags name."""
    if pg.enabled():
        heads = await pg_memory.consolidated_heads(org, namespace, source_key)
        return {identity_key(h): _sources(h["tags"]) for h in heads}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, tags
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND has(tags, 'consolidated') AND has(tags, {{srctag:String}})
          AND tombstoned_at IS NULL
        {_SEQUENTIAL}
        """,
        params={"org": org, "ns": namespace, "srctag": f"src:{source_key}"},
    )
    return {identity_key(r): _sources(r["tags"]) for r in rows}


async def live_source_keys(
    org: str, namespace: str, keys: list[str], *, ch: _CHClient | None = None
) -> set[str]:
    """Which of `keys` have a live head in `namespace`, read from the authority."""
    if not keys:
        return set()
    if pg.enabled():
        return await pg_memory.live_keys(org, namespace, keys)
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT memory_key FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL
        {_SEQUENTIAL}
        """,
        params={"org": org, "ns": namespace, "keys": keys},
    )
    return {str(r["memory_key"]) for r in rows}


def _sources(tags: Sequence[Any] | None) -> frozenset[str]:
    return frozenset(str(t)[4:] for t in (tags or []) if str(t).startswith("src:"))


async def downgrade_consolidated_dependents(
    org: str, namespace: str, source_key: str, *, ch: _CHClient | None = None
) -> tuple[int, int]:
    """A source memory was deleted. Every live summary built from it either
    goes with it (none of its sources survive) or is downgraded to
    `conflicted` with a `stale-source:<key>` tag so the review queue sees
    that it now says something its evidence no longer supports. Each is judged on its
    head as the authority holds it and written over a head that still cites the sources
    judged: a copy since (a lock, a touch) is written over, above it; a summary rebuilt
    from other sources is left. Under Postgres a batch whose head moved since its read (a
    delete of another of its sources, judging it at once) is read and judged again.
    Returns (tombstoned, downgraded), the rows written. Best effort; the caller never
    fails on it."""
    ch = ch or _default_client()
    written: set[IdentityKey] = set()
    tombstoned = downgraded = 0
    for _ in range(settings.pg_admission_rounds):
        gone, flagged, moved = await _judge_dependents(org, namespace, source_key, written, ch=ch)
        written |= gone | flagged
        tombstoned, downgraded = tombstoned + len(gone), downgraded + len(flagged)
        if not moved:
            break
    else:
        _log.warning("summaries of %s kept changing; some are left as they were", source_key)
    return tombstoned, downgraded


async def _judge_dependents(
    org: str, namespace: str, source_key: str, done: set[IdentityKey], *, ch: _CHClient
) -> tuple[set[IdentityKey], set[IdentityKey], bool]:
    """One round of `downgrade_consolidated_dependents` over the summaries not `done`:
    the keys tombstoned, the keys downgraded, and whether a moved head refused a batch.
    A summary is judged by its key: a namespace restored into itself holds one memory id
    under two keys."""
    found = await consolidated_dependents_of(org, namespace, source_key, ch=ch)
    judged = {k: srcs for k, srcs in found.items() if k not in done}
    if not judged:
        return set(), set(), False
    live = await live_source_keys(org, namespace, sorted(set().union(*judged.values())), ch=ch)
    orphaned = [k for k, srcs in judged.items() if not srcs & live]
    stale = [k for k, srcs in judged.items() if srcs & live]

    def rebuilt(row: MemoryRow) -> bool:
        return _sources(row.tags) != judged[identity_key(row)]

    now = naive_utc_now()
    stale_tag = f"stale-source:{source_key}"
    gone, moved = await _write_dependents(
        orphaned,
        lambda row: {"tombstoned_at": now, "invalid_at": now},
        skip=rebuilt,
        ch=ch,
    )
    flagged, moved_too = await _write_dependents(
        stale,
        lambda row: {
            "status": "conflicted",
            "tags": [*[t for t in (row.tags or []) if t != stale_tag], stale_tag],
        },
        skip=rebuilt,
        ch=ch,
    )
    return gone, flagged, moved or moved_too


async def _write_dependents(
    keys: list[IdentityKey],
    change: Callable[[MemoryRow], dict[str, Any]],
    *,
    skip: Callable[[MemoryRow], bool],
    ch: _CHClient,
) -> tuple[set[IdentityKey], bool]:
    """Write `change` over each summary's head, read by its key, and onto its vectors at
    the same version, under Postgres within the commit's lease (none for a redacted head,
    whose purge is their only writer). A tombstone retires the vectors with no text and a
    downgrade copies only those of its head's own hash (none for a head written before
    hashes), so neither carries text a redaction of another version may be purging.
    Under Postgres an embedded downgrade owes its search document until ClickHouse holds
    it, else the delivery worker builds it. Best effort: the keys written, and whether the
    batch was refused because a head moved since its read (Postgres)."""
    if not keys:
        return set(), False
    written: set[IdentityKey] = set()
    vectors: dict[IdentityKey, dict[str, Any]] = {}
    retired: list[tuple[IdentityKey, int]] = []
    of_content: dict[IdentityKey, str] = {}
    documented: list[bool] = []
    rows: list[dict[str, Any]] = []
    read: list[MemoryRow] = []
    committed = False

    async def copy_vectors() -> None:
        # Under Postgres `then` reads sequentially already; on ClickHouse nothing does.
        with sequential_reads():
            await reemit_embedding_rows_for_keys(
                list(vectors.items()), ch=ch, of_content=of_content
            )
            await retire_vectors_for_keys(retired, ch=ch, bare=True)

    def on_commit() -> None:
        nonlocal committed
        committed = True

    try:
        found = await memory_rows_by_identity_keys(keys, ch=ch, sequential=True)
        for k in keys:
            head = found.get(k)
            if head is None or skip(head):
                continue
            delta = {**change(head), "version": version_over(head)}
            row = build_memory_version_row(head, **delta)
            rows.append(row)
            read.append(head)
            written.add(k)
            sha = str(head.content_sha256 or "")
            if row["redacted_at"] is not None:
                pass  # its purge is the only writer of a redacted head's vectors
            elif row["tombstoned_at"] is not None:
                retired.append((k, int(row["version"])))
            elif is_content_hash(sha):
                # the head's own invalid_at, as a document built from it carries
                vectors[k] = {"invalid_at": head.invalid_at, **delta}
                of_content[k] = sha
            documented.append(row["tombstoned_at"] is None and pg_memory.embedded(row))
        if rows:
            await insert_versions_over_heads(
                rows,
                read,
                ch=ch,
                then=copy_vectors,
                documented=documented,
                settle_held=True,
                on_commit=on_commit,
            )
    except pg_memory.AdmissionContendedError:
        return set(), True
    except Exception as e:  # noqa: BLE001 — a source's delete never fails on its summaries
        if not committed:
            _log.warning("summaries of a deleted source not written: %s", error_summary(e))
            return set(), False
        # Under Postgres a downgrade's document stays owed; a tombstone's vectors are
        # retired by orphan_vector_reconcile, and on ClickHouse a downgrade's status waits
        # for `loom reconcile-status`.
        _log.warning("summaries written; their vectors were not copied: %s", error_summary(e))
    return written, False


# Tombstoned rows one page of `bulk_tombstone_scope`'s callback carries.
# not a setting: an in-process buffer, the rows one page holds and one outbox insert takes
CLEAR_PAGE_ROWS = 1000


async def bulk_tombstone_scope(
    *,
    org: str,
    namespace: str,
    user_id: str | None = None,
    agent: str | None = None,
    on_tombstoned: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """`memory.clear`: bulk-tombstone every live memory row matching
    (org, namespace, user_id?, agent?), plus the loom.memory_embeddings rows.

    The handler requires a concrete `user_id` or `agent` other than `'-'`
    (`"*"` arrives as unset); the repo runs the query as given (a scope-less
    call would clear the whole namespace — that's `erasure.request_namespace`'s job).

    `on_tombstoned`, when given, is called after the memory mutation and before
    the embeddings one, with each page of the memory rows it tombstoned
    (identity tuple, memory_key, memory_id and version), for the handler's
    webhook events; without it no row is read. A failed read-back is noted as
    a degradation, never raised.

    Returns {table_name: rows_tombstoned}.
    """
    ch = ch or _default_client()
    if pg.enabled():
        # The authority's heads take the clear as tombstone revisions and their vectors
        # are retired by version: nothing is mutated in place.
        memory_n, vectors_n, locked_n = await _tombstone_heads(
            org, namespace, user_id, agent, naive_utc_now_seconds(), on_tombstoned, ch=ch
        )
        return {
            "loom.memory": memory_n,
            "loom.memory_embeddings": vectors_n,
            "locked_skipped": locked_n,
        }

    scope = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if user_id is not None:
        scope.append("user_id = {uid:String}")
        params["uid"] = user_id
    if agent is not None:
        scope.append("agent = {agent:String}")
        params["agent"] = agent
    scope_sql = " AND ".join([*scope, "tombstoned_at IS NULL"])
    # Locked rows survive a clear. memory_embeddings has no lock columns, so it excludes
    # them by full identity: a lock under one identity must not keep another's vectors live.
    # The skipped rows are counted under `locked_skipped`.
    from server.memory.domain.lock import locked_predicate, unlocked_predicate

    # One timestamp for the whole clear, so the rows it tombstoned can be read back; and a
    # bound one, as a mutation on a replicated table refuses now().
    params["at"] = naive_utc_now_seconds()
    locked = locked_predicate(now="{at:DateTime}")
    locked_row = await ch.query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {scope_sql} AND {locked}",
        params=params,
    )
    locked_n = int((locked_row or {}).get("n", 0) or 0)
    counts = {
        "loom.memory": await _tombstone_where(
            "loom.memory",
            f"{scope_sql} AND {unlocked_predicate(now='{at:DateTime}')}",
            params,
            ch=ch,
        )
    }
    # Announced before the embeddings step, so a failure there cannot lose
    # the events: a retry finds no live memory rows and announces nothing.
    if on_tombstoned is not None and counts["loom.memory"]:
        try:
            await _page_tombstoned(" AND ".join(scope), params, on_tombstoned, ch=ch)
        except Exception as e:  # noqa: BLE001 — the rows are tombstoned; the clear must not fail
            degradations.note("memory.webhook", "not_announced", error_summary(e), health=False)
    parts = (
        await _unlocked_parts(scope_sql, locked, params, ch=ch) if locked_n else [(scope_sql, {})]
    )
    counts["loom.memory_embeddings"] = 0
    for where_sql, extra in parts:
        counts["loom.memory_embeddings"] += await _tombstone_where(
            "loom.memory_embeddings", where_sql, {**params, **extra}, ch=ch
        )
    counts["locked_skipped"] = locked_n
    return counts


async def _tombstone_where(
    table: str, where_sql: str, params: dict[str, Any], *, ch: _CHClient
) -> int:
    """Tombstone the live rows of `table` that match, and return how many there were."""
    row = await ch.query_one(
        f"SELECT count() AS n FROM {table} FINAL WHERE {where_sql}", params=params
    )
    n = int((row or {}).get("n", 0) or 0)
    if n:
        await ch.execute(
            f"""
            ALTER TABLE {table}
            UPDATE tombstoned_at = {{at:DateTime}}
            WHERE {where_sql}
            SETTINGS mutations_sync = 2
            """,
            params=params,
        )
    return n


# ~21 bytes per rendered hash: a bucket's list stays well inside one 128 KiB field
# and small in Keeper.
_LOCKED_PER_MUTATION = 2_500  # not a setting: a bound of the ClickHouse form field
_IDENTITY_HASH = "cityHash64(agent, user_id, session_id, memory_key)"


async def _unlocked_parts(
    scope_sql: str, locked: str, params: dict[str, Any], *, ch: _CHClient
) -> list[tuple[str, dict[str, Any]]]:
    """Predicates, with their params, that together cover the embedding rows whose identity
    no locked memory row in scope shares. A mutation takes no subquery, so the locked
    identities are read first and named by hash; past `_LOCKED_PER_MUTATION` the scope is
    split into hash buckets, each naming only its own locked identities."""
    rows = await ch.query_all(
        f"SELECT DISTINCT {_IDENTITY_HASH} AS h FROM loom.memory FINAL"
        f" WHERE {scope_sql} AND {locked}",
        params=params,
    )
    hashes = sorted({int(r["h"]) for r in rows})
    buckets = max(1, math.ceil(len(hashes) / _LOCKED_PER_MUTATION))
    unlocked = f"{scope_sql} AND {_IDENTITY_HASH} NOT IN {{locked:Array(UInt64)}}"
    parts = []
    for b in range(buckets):
        where = unlocked
        extra: dict[str, Any] = {"locked": [h for h in hashes if h % buckets == b]}
        if buckets > 1:
            where += f" AND {_IDENTITY_HASH} % {{buckets:UInt32}} = {{bucket:UInt32}}"
            extra |= {"buckets": buckets, "bucket": b}
        parts.append((where, extra))
    return parts


async def _tombstone_heads(
    org: str,
    namespace: str,
    user_id: str | None,
    agent: str | None,
    at: datetime,
    on_tombstoned: Callable[[list[dict[str, Any]]], Awaitable[None]] | None,
    *,
    ch: _CHClient,
) -> tuple[int, int, int]:
    """`bulk_tombstone_scope` under the Postgres authority: a tombstone version of every
    unlocked live head in scope, a page at a time in key order, each page committed
    with its heads rechecked (a lock since leaves its key alone; any other change reads the
    page again) and announced.
    Each tombstone retires its key's vectors at its own version, so a search document
    a writer still owes, at an older version, lands under it. Returns (memory rows,
    vector rows) tombstoned and the locked heads left."""
    from server.memory.domain.lock import effective_mode

    total = vectors = locked = 0
    after: IdentityKey | None = None
    rereads = 0
    while True:
        page = await pg_memory.live_heads_in(
            org, namespace, user_id=user_id, agent=agent, after=after, limit=CLEAR_PAGE_ROWS
        )
        if not page:
            return total, vectors, locked
        heads = {identity_key(h): _hydrate(h) for h in page}
        rows = [
            build_memory_version_row(h, tombstoned_at=at, version=version_over(h))
            for h in heads.values()
            if not effective_mode(h)
        ]
        if rows:
            try:
                committed = await pg_publish.commit(
                    rows,
                    admission=pg_memory.Admission(
                        recheck=pg_memory.heads_unchanged(heads, exact=True)
                    ),
                )
            except pg_memory.HeadMoved:
                rereads += 1
                if rereads >= settings.pg_admission_rounds:
                    raise pg_memory.AdmissionContendedError(
                        "the memories in scope kept changing"
                    ) from None
                continue  # a head of the page changed since it was read: read it again
            try:
                await pg_publish.publish(committed, ch=ch)
            except pg_lease.PublicationPending:
                # Committed, so done: search serves no hit of a tombstoned head meanwhile.
                _log.info("a cleared page is left for the delivery worker")
            total += len(rows)
        locked += len(heads) - len(rows)
        after, rereads = identity_key(page[-1]), 0
        if not rows:
            continue
        # Announced before its vectors retire, so a failure there cannot lose the events:
        # a retry finds no live head in the page.
        if on_tombstoned is not None:
            try:
                await on_tombstoned([{c: r[c] for c in _ANNOUNCED} for r in committed.rows])
            except Exception as e:  # noqa: BLE001 — the rows are tombstoned; the clear must not fail
                degradations.note("memory.webhook", "not_announced", error_summary(e), health=False)
        vectors += await retire_vectors_for_keys(
            [(identity_key(r), int(r["version"])) for r in committed.rows], ch=ch
        )


_ANNOUNCED = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "version",
)


async def _page_tombstoned(
    scope_sql: str,
    params: dict[str, Any],
    on_page: Callable[[list[dict[str, Any]]], Awaitable[None]],
    *,
    ch: Any,  # a CHClient: `raw_stream` is not on `_CHClient`
) -> None:
    """Hand `on_page` the memory rows in scope tombstoned at `params["at"]`, a page
    at a time in sort-key order. One streamed read, so the scope is scanned once
    however many pages it fills. A row a concurrent delete tombstoned in the same
    second comes too, at the version that delete announced, so its event repeats
    that one's id."""
    sql = f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, memory_id, version
        FROM loom.memory FINAL
        WHERE {scope_sql} AND tombstoned_at = {{at:DateTime}}
        ORDER BY agent, user_id, session_id, memory_key
        FORMAT JSONEachRow
    """
    page: list[dict[str, Any]] = []
    partial = b""  # a row split across two chunks
    async with ch.raw_stream(sql, params) as chunks:
        async for chunk in chunks:
            *lines, partial = (partial + chunk).split(b"\n")
            for line in lines:
                if not line:
                    continue
                row = json.loads(line)
                row["version"] = int(row["version"])  # JSONEachRow quotes a UInt64
                page.append(row)
                if len(page) == CLEAR_PAGE_ROWS:
                    await on_page(page)
                    page = []
    if page:
        await on_page(page)


async def tombstone_cascade_by_ids(
    org: str,
    memory_ids: list[str],
    *,
    when: datetime,
    update: Callable[[MemoryRow], dict[str, Any]] | None = None,
    ch: _CHClient | None = None,
    namespace: str,
    skip: Callable[[MemoryRow], bool] | None = None,
    strong: bool = False,
) -> list[dict[str, Any]]:
    """Tombstone rows by memory_id AND mirror the tombstone onto every tier
    search reads (`strong`: the rows read current, for a `skip` that must see a write
    just made).

    A tombstone that reaches only `loom.memory` is not a tombstone: search
    reads `loom.memory_embeddings` on its own predicates and never joins the
    memory table, so a row killed here alone keeps surfacing as an ANN
    candidate. This helper is the one funnel for by-id tombstones:

      1. a new RMT version per row with status='tombstoned', tombstoned_at
         set and expires_at cleared (every persisted column carried by
         build_memory_version_row; never an ALTER UPDATE, which mutates
         every version in place and skips the version discipline);
      2. the status mirror `write_versions_by_ids` runs, at that version, so
         the embeddings table collapses onto a tombstoned version too.

    `update(existing)` may add overrides (an audit note on source_trace); `skip(existing)`
    leaves a head as it is (the one the authority holds, which a caller may judge again).
    The embeddings mirror is best-effort: the memory tombstone is
    durable and the orphan_vector_reconcile worker heals a missed mirror.
    Returns each tombstoned row's identity tuple, memory_key, memory_id and
    tombstone version (`memory_change`), for the caller's webhook events."""
    ch = ch or _default_client()
    tombstoned: list[dict[str, Any]] = []
    decisions: set[str] = set()
    # The tombstones' version, which the vector retire carries: drawn on first use, once
    # every row is read, so a write that landed before the read sorts below it, and above
    # a head written on a replica whose clock is ahead.
    drawn: int | None = None
    decision_version = 0  # the highest of the decision rows' tombstones

    def _tombstone(existing: MemoryRow) -> dict[str, Any]:
        nonlocal drawn, decision_version
        if drawn is None:
            drawn = next_version()
        v = version_over(existing, drawn)
        tombstoned.append(memory_change(existing, v))
        if existing.memory_type == "decision":
            decisions.add(existing.memory_key)
            decision_version = max(decision_version, v)
        overrides: dict[str, Any] = {
            "status": "tombstoned",
            "tombstoned_at": when,
            "expires_at": None,
            "version": v,
        }
        if update is not None:
            overrides.update(update(existing))
        return overrides

    # `write_versions_by_ids` mirrors the tombstone onto memory_embeddings at its version.
    await write_versions_by_ids(
        org, memory_ids, _tombstone, ch=ch, namespace=namespace, skip=skip, strong=strong
    )
    # A decision's record goes with its writer's row, as on memory.delete; under Postgres
    # the tombstone's publication retires it (`pg_publish.retire_decision_records`).
    keys = sorted(decisions)
    if keys and not pg.enabled():
        try:
            await tombstone_decision_records(org, namespace, keys, version=decision_version, ch=ch)
        except Exception as e:  # noqa: BLE001 — the row tombstones stand
            _log.warning(
                "decision records not retired in %s/%s for %d key(s): %s",
                org,
                namespace,
                len(keys),
                error_summary(e),
            )
    return tombstoned


async def tombstone_by_id(
    org: str,
    memory_id: str,
    *,
    namespace: str,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
) -> list[dict[str, Any]]:
    """Tombstone `namespace`'s row of memory_id (UUID), cascading to the embeddings
    tier (see tombstone_cascade_by_ids), and return what that returns; `skip(head)`
    leaves the head, read current, as it is.

    Used by `memory.dedup_resolve` when the decision is to kill one
    side of the duplicate pair, keyed on the surrogate UUID rather
    than the identity tuple."""
    return await tombstone_cascade_by_ids(
        org,
        [memory_id],
        when=naive_utc_now(),
        ch=ch,
        namespace=namespace,
        skip=skip,
        strong=True,
    )


async def delete_procedure_runs_by_key(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete one procedure's run ledger. Called when the procedure row
    itself is deleted so its history does not outlive it."""
    return await delete_by_key(
        "loom.procedure_runs",
        "org = {org:String} AND namespace = {ns:String} AND memory_key = {key:String}",
        {"org": org, "ns": namespace, "key": memory_key},
        ch=ch,
    )
