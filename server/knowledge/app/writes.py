"""knowledge.add / upsert / bulk_add / update_metadata / delete / bulk_delete: documents and
their chunks written to `loom.documents` / `loom.chunks`, embedded inline by the configured
embedder.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from server.core.hashing import sha256_document_text as _content_sha256
from server.core.vectors import codes_state
from server.embedding import get_embedder
from server.knowledge.api.models_knowledge import (
    KnowledgeAddInput,
    KnowledgeAddOutput,
    KnowledgeBulkAddInput,
    KnowledgeBulkAddOutput,
    KnowledgeBulkDeleteInput,
    KnowledgeBulkDeleteOutput,
    KnowledgeDeleteInput,
    KnowledgeDeleteOutput,
    KnowledgeUpdateMetadataInput,
    KnowledgeUpdateMetadataOutput,
    KnowledgeUpsertInput,
    KnowledgeUpsertOutput,
    _KnowledgeBulkDeleteResult,
    _KnowledgeBulkEntryResult,
)
from server.knowledge.domain.chunking import Chunk, chunk_document

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from server.embedding import Embedder
    from server.knowledge.store.chunks import ChunkInsertRow
    from server.knowledge.store.documents import DocumentInsertRow, DocumentRow


def _nano_version() -> int:
    return time.time_ns()


def _blob_carries(prior: DocumentRow | None) -> bool:
    """Whether a write without fresh bytes keeps `prior`'s download: only from a live
    document. A deleted one (superseded_at) had its file removed unless another document
    shares it, and an erased one (blob_erased_at) had it removed by the erasure; carrying
    either would point a live row at a missing file (410 BLOB_GONE), and an erased blob
    carried back would count as erasure residue."""
    return prior is not None and prior.superseded_at is None and prior.blob_erased_at is None


async def _release_blob(org: str, namespace: str, blob_path: str, source_uri: str) -> None:
    """Delete the file at `blob_path` unless a live document other than
    `source_uri` shares it (blobs are content-addressed per namespace). A
    delete runs it after writing the superseded rows, so documents deleted
    together do not keep each other's file. Best-effort: an orphaned file is
    wasted disk, not a correctness bug."""
    from server.knowledge.store import documents as documents_repo
    from server.storage.blob_store import get_store

    try:
        if not await documents_repo.blob_shared_by_another_document(
            org, namespace, blob_path, source_uri
        ):
            await asyncio.to_thread(get_store().delete, blob_path)
    except Exception as e:  # noqa: BLE001 — blob cleanup is best-effort; orphan is only disk
        log.warning(
            "blob cleanup failed for %s/%s/%s (path=%s): %s",
            org,
            namespace,
            source_uri,
            blob_path,
            e,
        )


async def _resolve_blob(
    input: KnowledgeAddInput, prior: DocumentRow | None
) -> tuple[str, int, str, str]:
    """Resolve the blob columns for a knowledge write. Fresh bytes win and
    evict the superseded prior blob unless another document shares it;
    otherwise a prior blob carries forward so a metadata-only edit doesn't
    drop the user's download. Returns
    (blob_path, blob_size, blob_content_type, original_filename)."""
    prior_blob_path = (prior.blob_path if prior else "") or ""
    blob_path = ""
    blob_size = 0
    blob_content_type = (input.blob_content_type or "").strip()
    original_filename = input.original_filename or ""

    if input.original_bytes_b64:
        try:
            raw = base64.b64decode(input.original_bytes_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            from server.core.errors import ValidationError

            raise ValidationError(f"original_bytes_b64 is not valid base64: {e}") from e
        from server.storage.blob_store import get_store

        # File I/O with an fsync: off the event loop, which every other request shares.
        _sha, blob_path = await asyncio.to_thread(get_store().put, input.org, input.namespace, raw)
        blob_size = len(raw)
        if prior_blob_path and prior_blob_path != blob_path:
            await _release_blob(input.org, input.namespace, prior_blob_path, input.source_uri)
    elif prior_blob_path and _blob_carries(prior):
        blob_path = prior_blob_path
        blob_size = (prior.blob_size if prior else 0) or 0
        blob_content_type = blob_content_type or ((prior.blob_content_type if prior else "") or "")
        if not original_filename and prior:
            original_filename = prior.original_filename or ""
    return blob_path, blob_size, blob_content_type, original_filename


async def _embed_inline_batch(embedder: Embedder, contents: list[str]) -> list[list[float]]:
    """One upstream call for all of a document's chunks."""
    return await embedder.embed_many(contents) if contents else []


def _augment_for_embedding(
    chunk: str,
    *,
    title: str = "",
    description: str = "",
    section: str = "",
) -> str:
    """Prepend doc-level context to a chunk before embedding. The chunk's
    stored content stays unchanged (so search snippets read naturally) but
    its vector reflects title + description + the chunk's section breadcrumb
    ("Chapter 2 > Theories > Initiative vs Guilt"). No-op when
    all are empty."""
    title = (title or "").strip()
    description = (description or "").strip()
    section = (section or "").strip()
    if not title and not description and not section:
        return chunk
    header_parts = []
    if title:
        header_parts.append(title)
    if description:
        header_parts.append(description)
    if section:
        header_parts.append(section)
    return "\n".join(header_parts) + "\n\n" + chunk


def _chunk_text(content: str) -> list[Chunk]:
    """Chunk a document at the configured size and stride. Empty content is one empty chunk."""
    from server.settings import settings

    return chunk_document(
        content, size=settings.knowledge_chunk_size, stride=settings.knowledge_chunk_stride
    )


async def handle_knowledge_upsert(input: KnowledgeUpsertInput) -> KnowledgeUpsertOutput:
    """Insert new chunks for `source_uri`; soft-supersede prior chunks.

    Used when an upstream source changes after initial ingestion. Searches
    return the new content by default; superseded chunks remain queryable
    with explicit `include_superseded=true`.

    Dedup is warn-don't-block: a canonical sha256 over content+title and
    over each chunk surfaces `duplicate_of_uri` (a live doc under a
    different `source_uri` with the same doc hash) and
    `duplicate_chunk_count` (newly-inserted chunks whose hash already
    lives under a different source_uri).
    """
    from server.knowledge.store import chunks as knowledge_repo

    embedder = get_embedder()
    codes_state.refuse_width("chunks", embedder.dim)
    doc_id = str(uuid.uuid4())
    version = _nano_version()
    now = datetime.now(UTC).replace(tzinfo=None)

    doc_sha = _content_sha256(f"{input.title}\n{input.content}")
    dup_doc_row = await knowledge_repo.find_doc_by_content_sha256(
        input.org,
        input.namespace,
        doc_sha,
        excluding_source_uri=input.source_uri,
    )
    duplicate_of_uri = str(dup_doc_row["source_uri"]) if dup_doc_row else ""

    superseded_count = await knowledge_repo.count_active_for_doc(
        input.org,
        input.namespace,
        input.source_uri,
    )
    if superseded_count > 0:
        await knowledge_repo.mark_doc_superseded(
            input.org,
            input.namespace,
            input.source_uri,
            when=now,
        )

    # Blob handling — three cases:
    #   (a) caller supplied new bytes → write them, delete prior blob
    #       (if different) after the row lands.
    #   (b) no new bytes but prior doc had a blob → carry the prior
    #       blob_path forward (a metadata-only edit must not strip the
    #       user's download capability).
    #   (c) no new bytes and no prior blob → blob-less row, no cleanup.
    from server.knowledge.store import documents as _documents_repo

    prior = (
        await _documents_repo.get_one(
            input.org,
            input.namespace,
            input.source_uri,
            include_superseded=True,
        )
        if superseded_count > 0
        else None
    )
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(input, prior)

    from server.knowledge.store import documents as documents_repo

    await documents_repo.insert_one(
        documents_repo.build_document_row(
            org=input.org,
            namespace=input.namespace,
            source_uri=input.source_uri,
            doc_id=doc_id,
            title=input.title,
            description=input.description,
            content=input.content,
            tags=input.tags,
            metadata=json.dumps(input.metadata, sort_keys=True),
            version=version,
            ingested_at=now,
            blob_path=blob_path,
            blob_size=blob_size,
            blob_content_type=blob_content_type,
            original_filename=original_filename,
        )
    )

    chunks = _chunk_text(input.content)
    embed_inputs = [
        _augment_for_embedding(
            c.text,
            title=input.title,
            description=input.description,
            section=c.breadcrumb,
        )
        for c in chunks
    ]
    rows = knowledge_repo.build_chunk_rows(
        org=input.org,
        namespace=input.namespace,
        source_uri=input.source_uri,
        chunks=chunks,
        embeddings=await _embed_inline_batch(embedder, embed_inputs),
        tags=input.tags,
        metadata=json.dumps(input.metadata, sort_keys=True),
        doc_version=version,
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        now=now,
    )
    await knowledge_repo.insert_chunks(rows)

    duplicate_chunk_count = await knowledge_repo.count_matching_chunk_hashes(
        input.org,
        input.namespace,
        [r["content_sha256"] for r in rows],
        excluding_source_uri=input.source_uri,
    )

    return KnowledgeUpsertOutput(
        doc_id=doc_id,
        chunks_created=len(rows),
        chunks_superseded=superseded_count,
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        duplicate_of_uri=duplicate_of_uri,
        duplicate_chunk_count=duplicate_chunk_count,
        namespace=input.namespace,
        source_uri=input.source_uri,
    )


async def handle_knowledge_add(input: KnowledgeAddInput) -> KnowledgeAddOutput:
    from server.observability.otel import traced

    embedder = get_embedder()
    codes_state.refuse_width("chunks", embedder.dim)
    doc_id = str(uuid.uuid4())
    version = _nano_version()
    now = datetime.now(UTC).replace(tzinfo=None)

    with traced("chunk.text", content_len=len(input.content)):
        chunks = _chunk_text(input.content)

    # Carry a prior blob forward across a metadata-only edit (no fresh
    # bytes) so the user's download capability isn't silently dropped.
    from server.knowledge.store import documents as _documents_repo

    prior = await _documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(input, prior)

    # The builder sets every loom.documents column, content_sha256 included
    # so cross-URI dedup can match.
    doc_row = _documents_repo.build_document_row(
        org=input.org,
        namespace=input.namespace,
        source_uri=input.source_uri,
        doc_id=doc_id,
        title=input.title,
        description=input.description,
        content=input.content,
        tags=input.tags,
        metadata=json.dumps(input.metadata, sort_keys=True),
        version=version,
        ingested_at=now,
        blob_path=blob_path,
        blob_size=blob_size,
        blob_content_type=blob_content_type,
        original_filename=original_filename,
    )

    from server.knowledge.store import chunks as knowledge_repo

    with traced("embed.chunks", model=embedder.model_name, chunk_count=len(chunks)):
        embed_inputs = [
            _augment_for_embedding(
                c.text,
                title=input.title,
                description=input.description,
                section=c.breadcrumb,
            )
            for c in chunks
        ]
        rows = knowledge_repo.build_chunk_rows(
            org=input.org,
            namespace=input.namespace,
            source_uri=input.source_uri,
            chunks=chunks,
            embeddings=await _embed_inline_batch(embedder, embed_inputs),
            tags=input.tags,
            metadata=json.dumps(input.metadata, sort_keys=True),
            doc_version=version,
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            now=now,
        )

    # documents + chunks INSERTs are independent — run them concurrently.
    from server.knowledge.store import documents as documents_repo

    async def _insert_doc() -> None:
        with traced("ch.insert.documents", table="loom.documents"):
            await documents_repo.insert_one(doc_row)

    async def _insert_chunks() -> None:
        with traced("ch.insert.chunks", table="loom.chunks", chunk_count=len(rows)):
            await knowledge_repo.insert_chunks(rows)

    with traced("ch.insert.dual_write", concurrent=2):
        await asyncio.gather(_insert_doc(), _insert_chunks())

    return KnowledgeAddOutput(
        doc_id=doc_id,
        chunks_created=len(rows),
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        namespace=input.namespace,
        source_uri=input.source_uri,
    )


# ============================================================
# knowledge.delete — per-(namespace, source_uri) soft-delete
# ============================================================


async def handle_knowledge_delete(
    input: KnowledgeDeleteInput,
) -> KnowledgeDeleteOutput:
    """Tombstone all live chunks for a source_uri and supersede the document.

    Idempotent: if no live chunks exist, returns deleted=False with zero
    counts (no ALTER UPDATE, no document write). Re-deleting an already-
    deleted document is a cheap COUNT().
    """
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    now = datetime.now(UTC).replace(tzinfo=None)

    active = await knowledge_repo.count_active_for_doc(
        input.org,
        input.namespace,
        input.source_uri,
    )

    # Look up the doc row regardless of chunk count: a doc whose chunks
    # were never embedded still needs superseded_at set, or it lingers
    # in the Library list. Delete must end with the doc row superseded
    # regardless of chunk state.
    doc = await documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    if doc is None and active == 0:
        return KnowledgeDeleteOutput(deleted=False, chunks_tombstoned=0)

    # Re-delete of an already-deleted doc: deleted=False lets callers
    # distinguish a real first delete from a re-ask on something gone.
    if active == 0 and doc is not None and doc.superseded_at is not None:
        return KnowledgeDeleteOutput(deleted=False, chunks_tombstoned=0)

    if active > 0:
        await knowledge_repo.mark_doc_superseded(
            input.org,
            input.namespace,
            input.source_uri,
            when=now,
        )

    if doc is not None and doc.superseded_at is None:
        await documents_repo.insert_one(
            documents_repo.build_document_row(doc, version=_nano_version(), superseded_at=now)
        )
        if doc.blob_path:
            await _release_blob(input.org, input.namespace, doc.blob_path, input.source_uri)

    return KnowledgeDeleteOutput(deleted=True, chunks_tombstoned=active)


# ============================================================
# knowledge.update_metadata — title / description / tags edit
# ============================================================


async def handle_knowledge_update_metadata(
    input: KnowledgeUpdateMetadataInput,
) -> KnowledgeUpdateMetadataOutput:
    """Update doc-level metadata without touching chunks.

    Insert a new ReplacingMergeTree(version) row on `documents` with
    edited fields applied and existing fields carried forward. Chunks
    stay untouched — their stored content is unchanged and their
    embeddings remain as-is. Caller's responsibility to know that
    embeddings won't reflect the new title/description until a
    re-upload (`knowledge.upsert`).
    """
    from server.knowledge.store import documents as documents_repo

    existing = await documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=False,
    )
    if existing is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOT_FOUND",
                "message": (
                    f"no live document with source_uri={input.source_uri!r} "
                    f"in {input.org}/{input.namespace}"
                ),
            },
        )

    next_title = existing.title if input.title is None else input.title
    next_description = existing.description if input.description is None else input.description
    next_tags = existing.tags if input.tags is None else input.tags

    # No-op if nothing changed, so idempotent re-applies don't pile up versions.
    if (
        next_title == existing.title
        and next_description == existing.description
        and list(next_tags) == list(existing.tags)
    ):
        return KnowledgeUpdateMetadataOutput(
            updated=False,
            title=existing.title,
            description=existing.description,
            tags=existing.tags,
        )

    await documents_repo.insert_one(
        documents_repo.build_document_row(
            existing,
            title=next_title,
            description=next_description,
            tags=next_tags,
            version=_nano_version(),
        )
    )

    chunks_reindexed = 0
    if input.reindex:
        # Re-run upsert with unchanged content but the new title +
        # description, so the chunk vectors carry the current augmentation.
        upsert_in = KnowledgeUpsertInput(
            org=input.org,
            namespace=input.namespace,
            source_uri=input.source_uri,
            content=existing.content,
            title=next_title,
            description=next_description,
            tags=list(next_tags),
            metadata=(json.loads(existing.metadata) if existing.metadata else {}),
        )
        upsert_out = await handle_knowledge_upsert(upsert_in)
        chunks_reindexed = upsert_out.chunks_superseded

    return KnowledgeUpdateMetadataOutput(
        updated=True,
        title=next_title,
        description=next_description,
        tags=list(next_tags),
        chunks_reindexed=chunks_reindexed,
    )


# ============================================================
# knowledge.bulk_add — batched ingestion
# ============================================================


async def handle_knowledge_bulk_add(
    input: KnowledgeBulkAddInput,
) -> KnowledgeBulkAddOutput:
    """One CH insert for documents, one for all chunks (across all entries).
    Chunking + embedding still per-doc per the configured policy."""

    embedder = get_embedder()
    codes_state.refuse_width("chunks", embedder.dim)
    now = datetime.now(UTC).replace(tzinfo=None)

    doc_rows: list[DocumentInsertRow] = []
    chunk_rows: list[ChunkInsertRow] = []
    results: list[_KnowledgeBulkEntryResult] = []

    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    # Like knowledge.add without fresh bytes: a re-added URI keeps the
    # prior document's download (blob columns carry forward).
    priors = {
        d.source_uri: d
        for d in await documents_repo.get_many_by_uris(
            input.org,
            input.namespace,
            list({e.source_uri for e in input.entries}),
            include_superseded=True,
        )
    }

    for entry in input.entries:
        doc_id = str(uuid.uuid4())
        version = _nano_version()
        chunks = _chunk_text(entry.content)
        prior = priors.get(entry.source_uri)
        prior = prior if _blob_carries(prior) else None

        doc_rows.append(
            documents_repo.build_document_row(
                org=input.org,
                namespace=input.namespace,
                source_uri=entry.source_uri,
                doc_id=doc_id,
                title=entry.title,
                description=entry.description,
                content=entry.content,
                tags=entry.tags,
                metadata=json.dumps(entry.metadata, sort_keys=True),
                version=version,
                ingested_at=now,
                blob_path=prior.blob_path if prior else "",
                blob_size=prior.blob_size if prior else 0,
                blob_content_type=prior.blob_content_type if prior else "",
                original_filename=prior.original_filename if prior else "",
            )
        )

        # Batched per-doc; cross-doc batching is not done here.
        embed_inputs = [
            _augment_for_embedding(
                c.text,
                title=entry.title,
                description=entry.description,
                section=c.breadcrumb,
            )
            for c in chunks
        ]
        chunk_rows.extend(
            knowledge_repo.build_chunk_rows(
                org=input.org,
                namespace=input.namespace,
                source_uri=entry.source_uri,
                chunks=chunks,
                embeddings=await _embed_inline_batch(embedder, embed_inputs),
                tags=entry.tags,
                metadata=json.dumps(entry.metadata, sort_keys=True),
                doc_version=version,
                embedding_model=embedder.model_name,
                embedding_dim=embedder.dim,
                now=now,
            )
        )

        results.append(
            _KnowledgeBulkEntryResult(
                source_uri=entry.source_uri,
                doc_id=doc_id,
                chunks_created=len(chunks),
            )
        )

    await documents_repo.insert_many(doc_rows)
    await knowledge_repo.insert_chunks(chunk_rows)

    return KnowledgeBulkAddOutput(
        written=results,
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
    )


# ============================================================
# knowledge.bulk_delete — batched tombstone
# ============================================================


async def handle_knowledge_bulk_delete(
    input: KnowledgeBulkDeleteInput,
) -> KnowledgeBulkDeleteOutput:
    """Tombstone all chunks for the listed source_uris in one ALTER UPDATE,
    plus per-uri chunk counts (one SELECT first). Document rows are
    superseded via a single batched INSERT."""
    from server.knowledge.store import chunks as knowledge_repo

    now = datetime.now(UTC).replace(tzinfo=None)

    counts = await knowledge_repo.count_active_by_uri(
        input.org,
        input.namespace,
        input.source_uris,
    )
    total = sum(counts.values())

    if total > 0:
        await knowledge_repo.mark_docs_superseded(
            input.org,
            input.namespace,
            input.source_uris,
            when=now,
        )

        # Supersede document rows: read latest rows, re-INSERT with
        # superseded_at set + bumped versions.
        from server.knowledge.store import documents as documents_repo

        existing_docs = await documents_repo.get_many_by_uris(
            input.org,
            input.namespace,
            input.source_uris,
        )
        doc_rows = [
            documents_repo.build_document_row(
                d,
                version=_nano_version() + i,  # unique per row
                superseded_at=now,
            )
            for i, d in enumerate(existing_docs)
        ]
        await documents_repo.insert_many(doc_rows)
        # With every row of the batch superseded, a file only batch members
        # shared is released too.
        for d in existing_docs:
            if d.blob_path:
                await _release_blob(input.org, input.namespace, d.blob_path, d.source_uri)

    deleted_results = [
        _KnowledgeBulkDeleteResult(
            source_uri=uri,
            chunks_tombstoned=counts.get(uri, 0),
        )
        for uri in input.source_uris
    ]
    return KnowledgeBulkDeleteOutput(
        deleted=deleted_results,
        total_chunks_tombstoned=total,
    )
