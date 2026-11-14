"""knowledge.move: documents moved to another namespace of the org, each with its chunks
and their vectors, its original file, its tables and its metadata. The source keeps the
document deleted, as `knowledge.delete` leaves it, so `knowledge.restore` there brings it
back."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC
from typing import Any

from server.auth.context import current_auth
from server.auth.gateway import personal_namespace
from server.core.ch import own_write_reads
from server.core.errors import ValidationError
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover
from server.knowledge.api.models_knowledge import (
    KnowledgeDeleteInput,
    KnowledgeMoveInput,
    KnowledgeMoveOutput,
    _KnowledgeMoveResult,
)
from server.knowledge.app import ingest_run, writes
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store import blobs
from server.knowledge.store import chunks as chunks_repo
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import restore as restore_repo
from server.knowledge.store.documents import DocumentRow
from server.memory.app.provenance import writer_actor

# What a document row says of an original the move could not read (the sweep took it).
_NO_ORIGINAL = {"blob_path": "", "blob_size": 0, "blob_content_type": "", "original_filename": ""}


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
    with own_write_reads():  # a document written a call ago moves too
        results = [
            await _move(input.org, input.namespace, to, uri)
            for uri in dict.fromkeys(input.source_uris)
        ]
    return KnowledgeMoveOutput(found=True, to_namespace=to, results=results)


async def _move(org: str, src: str, dst: str, uri: str) -> _KnowledgeMoveResult:
    """One document, under the source's claim in both namespaces, taken in one order so
    two moves between the same places never wait on each other."""
    first, second = sorted((src, dst))
    async with (
        ingest_run.claim_source(org, first, uri) as one,
        ingest_run.claim_source(org, second, uri) as two,
    ):
        if not (one and two):
            return _KnowledgeMoveResult(source_uri=uri, reason="busy")
        doc = await documents_repo.get_one(org, src, uri)
        if doc is None:
            return _KnowledgeMoveResult(source_uri=uri, reason="not_found")
        # A source sync owns it: its next pass would write it back where it was.
        if doc.actor == SYNC_WORKER_ACTOR:
            return _KnowledgeMoveResult(source_uri=uri, reason="synced")
        for ns in (src, dst):
            if await documents_repo.publication_may_land(org, ns, uri):
                return _KnowledgeMoveResult(source_uri=uri, reason="busy")
        there = await documents_repo.get_one(org, dst, uri, include_superseded=True)
        if there is not None and there.superseded_at is None:
            # The same text there is an earlier move that stopped before its delete.
            if there.content_sha256 != doc.content_sha256 or there.title != doc.title:
                return _KnowledgeMoveResult(source_uri=uri, reason="exists")
        elif not await _copy(doc, dst, there):
            return _KnowledgeMoveResult(source_uri=uri, reason="erased")
        await writes.delete_document(KnowledgeDeleteInput(org=org, namespace=src, source_uri=uri))
    return _KnowledgeMoveResult(source_uri=uri, moved=True)


async def _copy(doc: DocumentRow, dst: str, there: DocumentRow | None) -> bool:
    """Write `doc` into `dst` over `there` (a deleted document at its source, or none):
    originals, chunks, tables, then the row. False, with nothing left behind, when an
    erasure reaches the document or the place it goes."""
    started = int(time.time())
    if await _erasing(doc, dst, started):
        return False
    version = documents_repo.version_past(max(doc.version, there.version if there else 0))
    revision = writes.next_revision(there)
    originals = await _copy_originals(doc, dst)
    rows = await chunks_repo.rows_of_version(doc.org, doc.namespace, doc.source_uri, doc.version)
    tables: list[dict[str, Any]] = []
    try:
        await chunks_repo.insert_chunks(
            [
                {**r, "namespace": dst, "doc_version": version, "chunk_metadata": _at(r, revision)}
                for r in rows
            ]
        )
        await _copy_tables(doc, dst, version, tables)
        await documents_repo.insert_one(
            documents_repo.build_document_row(
                doc,
                namespace=dst,
                doc_id=str(uuid.uuid4()),
                version=version,
                revision=revision,
                actor=writer_actor(),
                **originals,
            ),
            over=there.version if there else 0,
            retire_older=True,
            chunked=True,
        )
    except BaseException as e:  # a cancel too: what this copy wrote must not serve
        if not writes.published_anyway(e):
            await _forget(doc, dst, version, tables)
        raise
    # An erasure registered while this ran may have passed the rows already.
    if await _erasing(doc, dst, started):
        await documents_repo.drop_committed(doc.org, dst, doc.source_uri, version)
        await restore_repo.delete_row(doc.org, dst, doc.source_uri, version)
        await _forget(doc, dst, version, tables)
        return False
    return True


def _at(row: Any, revision: int) -> str:
    """A chunk's section metadata as the moved document's revision."""
    meta = json.loads(row["chunk_metadata"] or "{}")
    return json.dumps({**meta, "revision": revision}, sort_keys=True)


async def _copy_originals(doc: DocumentRow, dst: str) -> dict[str, Any]:
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
            cleared.update(_NO_ORIGINAL if column == "blob_path" else {column: ""})
            continue
        await blobs.put(doc.org, dst, data, uploader=doc.user_id)
    return cleared


async def _copy_tables(
    doc: DocumentRow, dst: str, version: int, copied: list[dict[str, Any]]
) -> None:
    """The tables the document's version serves, copied into `dst` at `version`, each as
    a new snapshot; `copied` gathers their catalog rows as they go, for a take-back."""
    for catalog in await datasets_repo.at_version(
        doc.org, doc.namespace, doc.source_uri, doc.version
    ):
        row = datasets_repo.dataset_row(
            catalog, namespace=dst, doc_version=version, snapshot_id=time.time_ns()
        )
        copied.append(row)
        if not await datasets_repo.copy_snapshot(
            catalog, namespace=dst, snapshot_id=row["snapshot_id"], doc_version=version
        ):
            raise RuntimeError(f"a table of {doc.source_uri} did not copy whole")
    await datasets_repo.insert_catalog(copied)


async def _forget(doc: DocumentRow, dst: str, version: int, tables: list[dict[str, Any]]) -> None:
    """Take back a copy that does not serve: its chunks and its tables. Its originals stay
    for the sweep, as any upload whose row did not land does."""
    await chunks_repo.delete_versions(doc.org, dst, [(doc.source_uri, version)])
    await datasets_repo.forget_published(doc.org, dst, {doc.source_uri: version})
    for t in tables:
        await datasets_repo.forget_snapshot(t)


async def _erasing(doc: DocumentRow, dst: str, since: int) -> bool:
    """Whether an unfinished erasure may reach the document where it is (registered
    since it was written) or the place it goes (registered since the move started)."""
    written = int(doc.ingested_at.replace(tzinfo=UTC).timestamp())
    for ns, from_s in ((doc.namespace, written), (dst, since)):
        rows = await ledger_reads.unfinished_reaching(
            doc.org, ns, doc.user_id, from_s, ledger_reads.LEDGER_CLOCK_MARGIN_S
        )
        if any(may_cover(str(r["scope_type"]), str(r["scope_value"]), ns) for r in rows):
            return True
    return False
