"""knowledge.move: documents moved to another namespace of the org, each with its chunks
and their vectors, its original file, its tables and its metadata. The source keeps the
document deleted, as `knowledge.delete` leaves it, so `knowledge.restore` there brings it
back. The ordering, a cut-off move and an erasure meanwhile are in
docs/architecture/failure-and-recovery.md, "Knowledge move"."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from server.auth.context import current_auth
from server.auth.gateway import personal_namespace
from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import ValidationError
from server.core.leases import LeaseUnavailable
from server.core.pg_lease import PublicationPending
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover
from server.knowledge.api.models_knowledge import (
    KnowledgeMoveInput,
    KnowledgeMoveOutput,
    MoveReason,
    _KnowledgeMoveResult,
)
from server.knowledge.app import ingest_jobs, ingest_run, writes
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store import blobs
from server.knowledge.store import chunks as chunks_repo
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import restore as restore_repo
from server.knowledge.store.documents import DocumentRow
from server.knowledge.store.pg_documents import HeadMoved
from server.memory.app.provenance import writer_actor


class _Retry(Exception):
    """A copy that did not land whole, or a write of either place since it was read: what
    this move wrote is taken back, and the source answers busy."""


# A source whose move raised one of these answers busy; the others still move.
_BUSY = (LeaseUnavailable, HeadMoved, _Retry)


# The sources one call moves at once.
_AT_ONCE = 4  # not a setting: bounds one request's concurrent statements


@dataclass(frozen=True)
class _Copy:
    """What a move wrote where it goes: the document at `version`, and its tables."""

    doc: DocumentRow
    dst: str
    version: int
    tables: list[dict[str, Any]]


async def handle_knowledge_move(input: KnowledgeMoveInput) -> KnowledgeMoveOutput:
    """Move each document of `source_uris` from `namespace` to `to_namespace`. The
    transport checked writer on `namespace`; `to_namespace` is checked here, and one the
    caller cannot write answers found=False, never a 403 that says it exists."""
    from server.auth.authz import Role, row_in_scope

    try:
        to = personal_namespace(current_auth(), input.org, input.to_namespace)
    except ValueError:
        raise ValidationError("to_namespace @me names an end user's own files") from None
    if to == input.namespace:
        raise ValidationError("to_namespace is the namespace the documents are in")
    if not await row_in_scope(input.org, to, Role.WRITER):
        return KnowledgeMoveOutput(found=False)
    uris = list(dict.fromkeys(input.source_uris))
    with own_write_reads():  # a document written a call ago moves too
        started = await blobs.ch_now_s()
        read = await documents_repo.get_many_by_uris(input.org, input.namespace, uris)
        docs = {d.source_uri: d for d in read}
        ledger = await _ledger(input.org, read, started)
        # A few sources at a time, each under its own claims. A failure that is not one
        # source's (`_BUSY`) cancels the others; a copy still in progress is taken back.
        slots = asyncio.Semaphore(_AT_ONCE)

        async def one(uri: str) -> tuple[MoveReason, _Copy | None]:
            async with slots:
                return await _move(docs.get(uri), to, ledger, started)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = {uri: group.create_task(one(uri)) for uri in uris}
        except ExceptionGroup as e:  # the request answers its first failure, as raised
            raise e.exceptions[0] from None
        reasons: dict[str, MoveReason] = {uri: t.result()[0] for uri, t in tasks.items()}
        copies = [c for t in tasks.values() if (c := t.result()[1]) is not None]
        # An erasure registered while this ran may have passed the copies already.
        if copies:
            ledger = await _ledger(input.org, [c.doc for c in copies], started)
            for c in copies:
                if _erasing(ledger, c.doc, to, started):
                    await asyncio.shield(_take_back(c))
                    reasons[c.doc.source_uri] = "erased"
    moved = [u for u in uris if not reasons[u]]
    if moved:
        from server.observability import audit

        # The transport's row is under the source namespace; this one is where they went.
        await audit.emit(
            event_type="knowledge.move",
            org=input.org,
            namespace=to,
            actor=writer_actor(),
            subject={"from_namespace": input.namespace, "source_uris": moved},
        )
    return KnowledgeMoveOutput(
        found=True,
        to_namespace=to,
        results=[
            _KnowledgeMoveResult(source_uri=u, moved=not reasons[u], reason=reasons[u])
            for u in uris
        ],
    )


async def _move(
    read: DocumentRow | None, dst: str, ledger: list[dict[str, Any]], started: int
) -> tuple[MoveReason, _Copy | None]:
    """One document: its reason ("" once moved) and what it wrote where it goes. Its
    originals are stored first, outside the claims; the checks, its chunks, tables and
    row, and its delete where it was run under the source's claim in both namespaces,
    taken in one order so two moves between the same places do not each hold one and
    give up on the other."""
    if read is None:
        return "not_found", None
    # A source sync owns it: its next pass would write it back where it was.
    if read.actor == SYNC_WORKER_ACTOR:
        return "synced", None
    if _erasing(ledger, read, dst, started):
        return "erased", None
    org, uri = read.org, read.source_uri
    try:
        # An original no row names yet is the sweep's, as any upload whose row did not land.
        originals = await _store_originals(read, dst)
        first, second = sorted((read.namespace, dst))
        async with ingest_run.claim_source(org, first, uri) as one:
            if not one:
                return "busy", None
            async with ingest_run.claim_source(org, second, uri) as two:
                if not two:
                    return "busy", None
                return await _claimed(read, dst, originals)
    except _BUSY:
        return "busy", None


async def _claimed(
    read: DocumentRow, dst: str, originals: dict[str, Any]
) -> tuple[MoveReason, _Copy | None]:
    org, src, uri = read.org, read.namespace, read.source_uri
    doc = await documents_repo.get_one(org, src, uri)
    if doc is None:
        return "not_found", None
    if doc.version != read.version:  # written since: its originals may be others
        return "busy", None
    for ns in (src, dst):
        if await documents_repo.publication_may_land(org, ns, uri):
            return "busy", None
    # An ingest of the source still to publish would write it back where it was.
    if await ingest_jobs.open_for_source(org, src, uri):
        return "busy", None
    there = await documents_repo.get_one(org, dst, uri, include_superseded=True)
    copy: _Copy | None = None
    if there is None or there.superseded_at is not None:
        copy = await _copy(doc, dst, there, originals)
    elif not _earlier_move(doc, there):
        return "exists", None
    try:
        if not await _delete_source(doc):
            raise _Retry
    except BaseException as e:  # a cancel too: a copy whose source stays live goes
        if copy is not None and not writes.published_anyway(e):
            await asyncio.shield(_take_back(copy))
        raise
    return "", copy


def _earlier_move(doc: DocumentRow, there: DocumentRow) -> bool:
    """Whether the live row where it goes is this document's copy, left by a move that
    stopped before its delete: a copy keeps the text, title, author and ingest time."""
    return (there.content_sha256, there.user_id, there.ingested_at) == (
        doc.content_sha256,
        doc.user_id,
        doc.ingested_at,
    )


async def _copy(
    doc: DocumentRow, dst: str, there: DocumentRow | None, originals: dict[str, Any]
) -> _Copy:
    """Write `doc` into `dst` over `there` (a deleted document at its source, or none):
    chunks, tables, then the row, each copied inside ClickHouse. Anything that does not
    land whole is taken back before the error answers."""
    over = documents_repo.version_of(there)
    version = documents_repo.version_past(max(doc.version, over))
    revision = writes.next_revision(there)
    catalogs = await datasets_repo.at_version(doc.org, doc.namespace, doc.source_uri, doc.version)
    copy = _Copy(doc, dst, version, _tables_of(catalogs, dst, version))
    # The mover is the actor of every row the move writes; the author, the content's
    # class and the ingest time stay, as the move rewrites no content.
    row = documents_repo.build_document_row(
        doc,
        namespace=dst,
        doc_id=str(uuid.uuid4()),
        version=version,
        revision=revision,
        actor=writer_actor(),
        **originals,
    )
    try:
        landed = await chunks_repo.copy_revision(
            doc.org,
            doc.namespace,
            doc.source_uri,
            revision=doc.revision,
            upto_version=doc.version,
            to_namespace=dst,
            to_version=version,
            to_revision=revision,
        )
        if not landed:
            raise _Retry
        for catalog, table in zip(catalogs, copy.tables, strict=True):
            if not await datasets_repo.copy_snapshot(
                catalog,
                namespace=dst,
                snapshot_id=table["snapshot_id"],
                doc_version=version,
                actor=table["actor"],
            ):
                raise _Retry
        await datasets_repo.insert_catalog(copy.tables)
        try:
            await documents_repo.insert_one(row, over=over, retire_older=True, chunked=True)
        except PublicationPending:
            pass  # committed: the delivery worker publishes it
        # Under the ClickHouse authority `over` is not checked: a write there since
        # `there` was read (a text write takes no claim) with a later version wins.
        head = await documents_repo.get_one(doc.org, dst, doc.source_uri, include_superseded=True)
        if documents_repo.version_of(head) != version:
            raise _Retry
    except BaseException as e:  # a cancel too: what this copy wrote must not serve
        if not writes.published_anyway(e):
            await asyncio.shield(_take_back(copy))
        raise
    return copy


def _tables_of(catalogs: list[dict[str, Any]], dst: str, version: int) -> list[dict[str, Any]]:
    """The catalog rows of the document's tables in `dst` at `version`, each a new
    snapshot."""
    first = time.time_ns()
    return [
        datasets_repo.dataset_row(
            c, namespace=dst, doc_version=version, snapshot_id=first + i, actor=writer_actor()
        )
        for i, c in enumerate(catalogs)
    ]


async def _delete_source(doc: DocumentRow) -> bool:
    """Delete `doc` where it was, as `knowledge.delete` does, over its own version: False
    when a write took the source since it was read (under the Postgres authority,
    HeadMoved)."""
    org, ns, uri = doc.org, doc.namespace, doc.source_uri
    # A text write takes no claim, and `over` is not checked under the ClickHouse authority.
    head = await documents_repo.get_one(org, ns, uri, include_superseded=True)
    if documents_repo.version_of(head) != doc.version:
        return False
    try:
        await documents_repo.insert_one(
            documents_repo.build_document_row(
                doc, version=documents_repo.version_past(doc.version), superseded_at=naive_utc_now()
            ),
            over=doc.version,
            retire_older=True,
        )
    except PublicationPending:
        pass  # committed: the delivery worker publishes it
    await datasets_repo.forget_published(org, ns, {uri: doc.version})
    return True


async def _take_back(copy: _Copy) -> None:
    """Remove what a move wrote where it goes: its row (and under the Postgres authority
    its commit), its chunks and its tables. Its originals stay for the sweep."""
    org, dst, uri, version = copy.doc.org, copy.dst, copy.doc.source_uri, copy.version
    await documents_repo.drop_committed(org, dst, uri, version)
    await restore_repo.delete_row(org, dst, uri, version)
    await chunks_repo.delete_versions(org, dst, [(uri, version)])
    await datasets_repo.forget(org, dst, [uri], only=version)
    for t in copy.tables:
        await datasets_repo.forget_snapshot(t)


async def _store_originals(doc: DocumentRow, dst: str) -> dict[str, Any]:
    """The document's original and its extraction artifact stored again in `dst` (an
    original belongs to its namespace: its sweep and erasure go by it); the columns to
    clear for one the source no longer holds."""
    cleared: dict[str, Any] = {}
    for column, sha in (("blob_path", doc.blob_path), ("artifact_sha256", doc.artifact_sha256)):
        if not sha:
            continue
        held = await blobs.locate(doc.org, doc.namespace, sha)
        data = await blobs.read(doc.org, doc.namespace, sha, stored=held) if held else None
        if data is None:
            cleared.update(documents_repo.NO_ORIGINAL if column == "blob_path" else {column: ""})
            continue
        await blobs.put(doc.org, dst, data, uploader=doc.user_id)
    return cleared


def _written_s(doc: DocumentRow) -> int:
    return int(doc.ingested_at.replace(tzinfo=UTC).timestamp())


async def _ledger(org: str, docs: Iterable[DocumentRow], started: int) -> list[dict[str, Any]]:
    """The org's erasures unfinished since the oldest of the documents was written or the
    move started, read once for every document of the call."""
    since = min([_written_s(d) for d in docs] + [started])
    return await ledger_reads.unfinished_since(org, since, ledger_reads.LEDGER_CLOCK_MARGIN_S)


def _erasing(ledger: list[dict[str, Any]], doc: DocumentRow, dst: str, started: int) -> bool:
    """Whether an erasure of `ledger` may reach the document where it is (unfinished when
    it was written) or the place it goes (unfinished when the move started)."""
    return any(
        _reaches(r, ns, doc.user_id, since)
        for r in ledger
        for ns, since in ((doc.namespace, _written_s(doc)), (dst, started))
    )


def _reaches(row: dict[str, Any], namespace: str, user_id: str, since_s: int) -> bool:
    """`ledger.unfinished_reaching`'s test for one row: unfinished at `since_s`, and of
    `namespace`, of `user_id`, or of a scope that may cover the namespace."""
    done = row.get("completed_at")
    if row["state"] == "completed" and (
        done is None
        or done.replace(tzinfo=UTC).timestamp() < since_s - ledger_reads.LEDGER_CLOCK_MARGIN_S
    ):
        return False
    scope, value = str(row["scope_type"]), str(row["scope_value"])
    if scope == "namespace":
        return value == namespace
    if scope == "user_id":
        return value == user_id
    return may_cover(scope, value, namespace)
