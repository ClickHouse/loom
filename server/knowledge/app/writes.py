"""knowledge.add / upsert / bulk_add / update_metadata / delete / bulk_delete: documents and
their chunks written to `loom.documents` / `loom.chunks`, embedded inline by the configured
embedder.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from server.core.errors import ValidationError
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
    KnowledgeIngestInput,
    KnowledgeUpdateMetadataInput,
    KnowledgeUpdateMetadataOutput,
    KnowledgeUpsertInput,
    KnowledgeUpsertOutput,
    _KnowledgeBulkDeleteResult,
    _KnowledgeBulkEntryResult,
)
from server.knowledge.domain.chunking import Chunk, chunk_context, chunk_document, embedding_text
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, source_tags

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from server.embedding import Embedder
    from server.knowledge.store.chunks import ChunkInsertRow
    from server.knowledge.store.documents import DocumentInsertRow, DocumentRow


def authorship(
    input: KnowledgeAddInput | KnowledgeBulkAddInput | KnowledgeIngestInput,
) -> dict[str, str]:
    """`authorship_of` the body's agent, session and declared class."""
    return authorship_of(
        agent=input.agent, session_id=input.session_id, source_class=input.source_class
    )


def authorship_of(*, agent: str, session_id: str, source_class: str | None) -> dict[str, str]:
    """Who writes a document and what it is. The end user comes from the
    request's identity, never its body (an end-user token's `sub`, a token's
    `read_user_id`), so no body can make a row erasable as someone else's; the
    agent is the one an end-user token signs in when it names one, else the
    body's; the session (a chat, an agent run) is the body's; the class is
    the operation's `asserted` unless the body declares a downgrade
    (`derived`, `external`, `imported`), as `memory.set` takes it."""
    from server.auth.context import current_auth
    from server.memory.app.provenance import writer_actor
    from server.memory.domain.provenance import source_class_for

    auth = current_auth()
    asserted = auth.agent_principal.removeprefix("agent:") if auth is not None else ""
    return {
        "agent": asserted or agent.strip() or "-",
        "user_id": (auth.read_user_id if auth is not None else "") or "-",
        "session_id": session_id.strip() or "-",
        "actor": writer_actor(),
        "source_class": source_class_for(default="asserted", requested=source_class),
    }


_AUTHOR_COLUMNS = ("agent", "user_id", "session_id", "actor", "source_class")


def _fresh_sha(input: KnowledgeAddInput) -> str:
    """The sha256 of the original a write uploads; '' for none (a malformed
    one is refused when it is stored)."""
    if not input.original_bytes_b64:
        return ""
    try:
        return hashlib.sha256(base64.b64decode(input.original_bytes_b64, validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        return ""


def _author_for(
    prior: DocumentRow | None, content: str, author: dict[str, str], fresh_sha: str = ""
) -> dict[str, str]:
    """`author`, unless the write leaves a live document as it was (a re-save,
    a title or tag edit, with no original or the same one): then the document
    stays whoever wrote that text and file, so the writer's erasure still
    finds it. A new original makes its uploader the author, whatever the text."""
    same_file = not fresh_sha or (prior is not None and fresh_sha == prior.blob_path)
    if prior is not None and prior.superseded_at is None and prior.content == content and same_file:
        return {c: str(getattr(prior, c)) for c in _AUTHOR_COLUMNS}
    return author


def _require_source_tags_kept(
    prior: DocumentRow | None, writer: str, author_actor: str, tags: list[str]
) -> None:
    """Refuse a change to the `source:*` tags of a sync-written document by another
    writer that keeps the worker as its author (see `SYNC_WORKER_ACTOR`)."""
    if prior is None or author_actor != SYNC_WORKER_ACTOR or writer == SYNC_WORKER_ACTOR:
        return
    if source_tags(tags) != source_tags(prior.tags):
        raise ValidationError(
            f"{prior.source_uri!r} was written by a document source's sync; its source:* "
            "tags belong to the sync and cannot be changed here",
            code="SOURCE_TAGS_LOCKED",
        )


def _nano_version() -> int:
    return time.time_ns()


def next_revision(prior: DocumentRow | None) -> int:
    """The revision a new version of the source publishes: one past the
    latest, deleted or not. Sequential writes never reuse one. Ingests of a
    source publish under its claim, one at a time; a text write takes no claim,
    so a text write racing another write of the same source can number the
    same revision (the later version row wins)."""
    return (prior.revision if prior is not None else 0) + 1


def _original_of(prior: DocumentRow | None, author: dict[str, str]) -> DocumentRow | None:
    """The prior row whose original a write without fresh bytes may carry:
    not one an end user uploaded when someone else writes new text, so that
    user's erasure finds every original of theirs."""
    if prior is not None and prior.user_id not in ("-", author["user_id"]):
        return None
    return prior


async def _resolve_blob(
    input: KnowledgeAddInput, prior: DocumentRow | None, *, uploader: str
) -> tuple[str, int, str, str]:
    """Resolve the blob columns for a knowledge write. Fresh bytes win (the
    prior original stays until the sweep finds no live document naming it, so a
    write that fails after this keeps the old download); otherwise a prior blob
    carries forward so a metadata-only edit doesn't drop the user's download.
    Only a live, unerased prior carries: a deleted one's original is the sweep's
    and an erased one's is gone (carrying it would answer 410 or be residue).
    `uploader` is the writing user's id (`authorship`), recorded with fresh
    bytes. Returns (blob_path, blob_size, blob_content_type, original_filename)."""
    from server.knowledge.store.documents import serves_original

    blob_path = ""
    blob_size = 0
    blob_content_type = (input.blob_content_type or "").strip()
    original_filename = input.original_filename or ""

    if input.original_bytes_b64:
        try:
            raw = base64.b64decode(input.original_bytes_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValidationError(f"original_bytes_b64 is not valid base64: {e}") from e
        from server.knowledge.store import blobs

        blob_path = await blobs.put(input.org, input.namespace, raw, uploader=uploader)
        blob_size = len(raw)
    elif prior is not None and serves_original(prior):
        blob_path = prior.blob_path
        blob_size = prior.blob_size or 0
        blob_content_type = blob_content_type or (prior.blob_content_type or "")
        original_filename = original_filename or (prior.original_filename or "")
    return blob_path, blob_size, blob_content_type, original_filename


async def _embed_inline_batch(embedder: Embedder, contents: list[str]) -> list[list[float]]:
    """One upstream call for all of a document's chunks."""
    return await embedder.embed_many(contents) if contents else []


def _contexts(chunks: list[Chunk], title: str, description: str) -> list[str]:
    """Each chunk's context (`chunk_context`): stored on its row and embedded before its
    text, which stays unchanged so search snippets read naturally."""
    return [chunk_context(title, description, c.breadcrumb) for c in chunks]


def _embedding_texts(chunks: list[Chunk], contexts: list[str]) -> list[str]:
    return [embedding_text(ctx, c.text) for c, ctx in zip(chunks, contexts, strict=True)]


def _chunk_text(content: str) -> list[Chunk]:
    """Chunk a document at the configured size and stride. Empty content is one empty chunk."""
    from server.settings import settings

    size = settings.knowledge_chunk_size
    # The two are set one at a time: a step past the window would skip text unindexed.
    stride = min(settings.knowledge_chunk_stride, size)
    return chunk_document(content, size=size, stride=stride)


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

    author = authorship(input)

    embedder = get_embedder()
    await codes_state.refuse_width("chunks", embedder.dim)
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
    # Blob handling — three cases:
    #   (a) caller supplied new bytes → write them; the sweep reclaims the
    #       prior blob once no live document names it.
    #   (b) no new bytes but prior doc had a blob → carry the prior
    #       blob_path forward (a metadata-only edit must not strip the
    #       user's download capability).
    #   (c) no new bytes and no prior blob → blob-less row, no cleanup.
    from server.knowledge.store import documents as _documents_repo

    # Read even when nothing is live: a deleted document's revision still counts.
    prior = await _documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    uploader = author["user_id"]  # the writer's, even when the document keeps its author
    writer = author["actor"]
    author = _author_for(prior, input.content, author, _fresh_sha(input))
    _require_source_tags_kept(prior, writer, author["actor"], input.tags)
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(
        input, _original_of(prior, author), uploader=uploader
    )
    # Only once the original is stored: a store that fails or is busy must leave the
    # current version searchable, not its chunks retired with no new version.
    if superseded_count > 0:
        await knowledge_repo.mark_doc_superseded(
            input.org,
            input.namespace,
            input.source_uri,
            when=now,
        )

    from server.knowledge.store import documents as documents_repo

    await documents_repo.insert_one(
        documents_repo.build_document_row(
            **author,
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
            revision=next_revision(prior),
        )
    )

    chunks = _chunk_text(input.content)
    contexts = _contexts(chunks, input.title, input.description)
    rows = knowledge_repo.build_chunk_rows(
        org=input.org,
        namespace=input.namespace,
        source_uri=input.source_uri,
        chunks=chunks,
        contexts=contexts,
        embeddings=await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts)),
        tags=input.tags,
        metadata=json.dumps(input.metadata, sort_keys=True),
        doc_version=version,
        revision=next_revision(prior),
        doc_format="",
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        now=now,
        author=author,
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

    author = authorship(input)

    embedder = get_embedder()
    await codes_state.refuse_width("chunks", embedder.dim)
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
    uploader = author["user_id"]  # the writer's, even when the document keeps its author
    writer = author["actor"]
    author = _author_for(prior, input.content, author, _fresh_sha(input))
    _require_source_tags_kept(prior, writer, author["actor"], input.tags)
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(
        input, _original_of(prior, author), uploader=uploader
    )

    # The builder sets every loom.documents column, content_sha256 included
    # so cross-URI dedup can match.
    doc_row = _documents_repo.build_document_row(
        **author,
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
        revision=next_revision(prior),
    )

    from server.knowledge.store import chunks as knowledge_repo

    with traced("embed.chunks", model=embedder.model_name, chunk_count=len(chunks)):
        contexts = _contexts(chunks, input.title, input.description)
        rows = knowledge_repo.build_chunk_rows(
            org=input.org,
            namespace=input.namespace,
            source_uri=input.source_uri,
            chunks=chunks,
            contexts=contexts,
            embeddings=await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts)),
            tags=input.tags,
            metadata=json.dumps(input.metadata, sort_keys=True),
            doc_version=version,
            revision=next_revision(prior),
            doc_format="",
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            now=now,
            author=author,
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
    stay untouched: their stored context (the old title and description)
    and their embeddings remain as-is, so both search legs keep matching
    the old context until `reindex=True` or a re-upload (`knowledge.upsert`).
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

    if input.reindex and existing.ingestion_id:
        # Its chunks were cut from the extracted artifact (keys, lines, blocks);
        # re-cutting the rendered text would turn it into a text document.
        raise ValidationError(
            "this document was ingested from a file: ingest it again (knowledge.ingest)"
            " to re-index it with the new title and description"
        )
    next_title = existing.title if input.title is None else input.title
    next_description = existing.description if input.description is None else input.description
    next_tags = existing.tags if input.tags is None else input.tags
    from server.memory.app.provenance import writer_actor

    _require_source_tags_kept(existing, writer_actor(), existing.actor, next_tags)

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
        # Re-run upsert with unchanged content but the new title + description, so
        # each chunk's context (embedded and lexically scored) is current.
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
        # The same text: `knowledge.upsert` keeps its author (`_author_for`).
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
    author = authorship(input)

    embedder = get_embedder()
    await codes_state.refuse_width("chunks", embedder.dim)
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

    # Every entry is checked before any is chunked or embedded: a refusal writes nothing.
    authors = []
    for entry in input.entries:
        prior_doc = priors.get(entry.source_uri)
        authors.append(_author_for(prior_doc, entry.content, author))
        _require_source_tags_kept(prior_doc, author["actor"], authors[-1]["actor"], entry.tags)

    for entry, entry_author in zip(input.entries, authors, strict=True):
        doc_id = str(uuid.uuid4())
        version = _nano_version()
        chunks = _chunk_text(entry.content)
        prior = _original_of(priors.get(entry.source_uri), entry_author)
        prior = prior if documents_repo.serves_original(prior) else None

        doc_rows.append(
            documents_repo.build_document_row(
                **entry_author,
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
                revision=next_revision(priors.get(entry.source_uri)),
            )
        )

        # Batched per-doc; cross-doc batching is not done here.
        contexts = _contexts(chunks, entry.title, entry.description)
        chunk_rows.extend(
            knowledge_repo.build_chunk_rows(
                org=input.org,
                namespace=input.namespace,
                source_uri=entry.source_uri,
                chunks=chunks,
                contexts=contexts,
                embeddings=await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts)),
                tags=entry.tags,
                metadata=json.dumps(entry.metadata, sort_keys=True),
                doc_version=version,
                revision=next_revision(priors.get(entry.source_uri)),
                doc_format="",
                embedding_model=embedder.model_name,
                embedding_dim=embedder.dim,
                now=now,
                author=entry_author,
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
