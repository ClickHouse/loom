"""knowledge.list / get / outline: reads of the documents of one namespace, and
`document_chunks`, a document's live chunks in order (knowledge.synthesize and
`loom decisions extract` read through it)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from server.core.cursors import decode_cursor as _decode_list_cursor
from server.core.cursors import encode_cursor as _encode_list_cursor
from server.core.tag_sql import list_tag_filter
from server.embedding import get_embedder
from server.knowledge.api.models_knowledge import (
    KnowledgeGetInput,
    KnowledgeGetOutput,
    KnowledgeListInput,
    KnowledgeListOutput,
    KnowledgeOutlineInput,
    KnowledgeOutlineOutput,
    _KnowledgeListEntry,
    _OutlineEntry,
)
from server.knowledge.domain.chunk_metadata import read_structure
from server.knowledge.domain.chunking import Chunk
from server.operations.read_projection import resolve as _resolve_projection

if TYPE_CHECKING:
    from server.memory.domain.corpus_synthesis import CorpusChunk

_LIST_ALWAYS = frozenset({"source_uri"})


async def document_tally(org: str, namespace: str) -> tuple[int, datetime | None]:
    """How many current documents the namespace holds, and the newest ingest."""
    from server.knowledge.store import documents as documents_repo

    return await documents_repo.current_document_tally(org, namespace)


# ============================================================
# knowledge.list — paginated curation listing
# ============================================================


async def handle_knowledge_list(
    input: KnowledgeListInput,
) -> KnowledgeListOutput:
    """Paginated source_uri listing scoped to one namespace.

    Joins `loom.documents FINAL` with a chunk-count aggregation. Keyset
    pagination on (ingested_at DESC, source_uri DESC) so the cursor is
    stable across inserts."""
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo

    cursor = _decode_list_cursor(input.cursor) if input.cursor else None
    with own_write_reads():  # a document added a call ago
        raw_rows = await documents_repo.list_with_chunk_counts(
            input.org,
            input.namespace,
            include_superseded=input.include_superseded,
            tag_filter=list_tag_filter(input.tags, every=input.tags_every),
            uri_prefixes=list(input.uri_prefix) if input.uri_prefix else None,
            cursor=cursor,
            limit=input.limit + 1,
        )
    has_more = len(raw_rows) > input.limit
    page = raw_rows[: input.limit]

    rows = [
        _KnowledgeListEntry(
            source_uri=r["source_uri"],
            title=r["title"] or "",
            description=r.get("description") or "",
            tags=list(r["tags"]),
            ingested_at=r["ingested_at"],
            chunk_count=int(r["chunk_count"]),
            superseded=r["superseded_at"] is not None,
            has_blob=bool(r.get("blob_path") or ""),
            blob_size=int(r.get("blob_size") or 0),
            blob_content_type=r.get("blob_content_type") or "",
            original_filename=r.get("original_filename") or "",
            content_preview=(r.get("content_preview") or "").strip(),
        )
        for r in page
    ]
    next_cursor = (
        _encode_list_cursor(page[-1]["ingested_at"], page[-1]["source_uri"])
        if rows and has_more
        else None
    )
    # The caller's field whitelist, resolved once.
    keep = _resolve_projection(_KnowledgeListEntry, input.fields, always=_LIST_ALWAYS)
    if keep is not None:
        for e in rows:
            e._projection = keep
    return KnowledgeListOutput(rows=rows, next_cursor=next_cursor)


async def handle_knowledge_outline(input: KnowledgeOutlineInput) -> KnowledgeOutlineOutput:
    """A document's table of contents from its chunks' section metadata, and
    optionally one section's text. Read-only; two cheap reads (document row,
    the document's chunks in index order)."""
    from server.core.ch import own_write_reads
    from server.knowledge.domain.chunking import Chunk, outline
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    with own_write_reads():
        doc = await documents_repo.get_one(input.org, input.namespace, input.source_uri)
    if doc is None:
        return KnowledgeOutlineOutput(found=False, source_uri=input.source_uri)
    rows = await knowledge_repo.list_for_doc(
        input.org,
        input.namespace,
        input.source_uri,
        embedding_model=get_embedder().model_name,
    )
    chunks: list[Chunk] = []
    for r in rows:
        meta = read_structure(r.chunk_metadata)
        chunks.append(
            Chunk(
                idx=r.chunk_idx,
                text=r.content,
                section_path=meta.section_path,
                section_depth=meta.section_depth,
                section_idx=meta.section_idx,
                char_start=meta.char_start,
            )
        )
    by_idx = {c.idx: c for c in chunks}
    entries = [
        _OutlineEntry(
            path=e["path"],
            title=e["title"],
            depth=e["depth"],
            chunk_idxs=e["chunk_idxs"],
            chars=sum(len(by_idx[i].text) for i in e["chunk_idxs"] if i in by_idx),
        )
        for e in outline(chunks)
    ]
    section_content: str | None = None
    section_idxs: list[int] = []
    if input.section_path:
        want = tuple(input.section_path)
        for c in chunks:
            if c.section_path == want or (
                input.include_children and c.section_path[: len(want)] == want
            ):
                section_idxs.append(c.idx)
        if section_idxs:
            section_content = "\n\n".join(by_idx[i].text for i in section_idxs)
    return KnowledgeOutlineOutput(
        found=True,
        source_uri=input.source_uri,
        title=doc.title or "",
        structured=bool(entries),
        chunk_count=len(chunks),
        sections=entries,
        section_content=section_content,
        section_chunk_idxs=section_idxs,
    )


async def handle_knowledge_get(input: KnowledgeGetInput) -> KnowledgeGetOutput:
    """Fetch one document. Excludes superseded rows by default."""
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo

    with own_write_reads():  # a get right after an add must see it
        row = await documents_repo.get_one(
            input.org,
            input.namespace,
            input.source_uri,
            include_superseded=input.include_superseded,
        )
    if row is None:
        return KnowledgeGetOutput(found=False, source_uri=input.source_uri)
    try:
        metadata = json.loads(row.metadata) if row.metadata else {}
    except json.JSONDecodeError:
        metadata = {}
    return KnowledgeGetOutput(
        found=True,
        source_uri=row.source_uri,
        title=row.title,
        content=row.content,
        tags=row.tags,
        metadata=metadata,
        ingested_at=row.ingested_at,
        superseded=row.superseded_at is not None,
        doc_id=row.doc_id,
        revision=row.revision,
        format=row.format or None,
        parser=row.parser or None,
        coverage=json.loads(row.coverage) if row.coverage else None,
    )


async def document_chunks(org: str, namespace: str, source_uri: str) -> list[CorpusChunk]:
    """The document's live chunks for the current embedding model, in document order, each
    with its section path, the revision it was written as and its locator. Empty for a
    document chunked under another model, until `loom rechunk`."""
    from server.knowledge.store import chunks as knowledge_repo
    from server.memory.domain.corpus_synthesis import CorpusChunk

    rows = await knowledge_repo.list_for_doc(
        org, namespace, source_uri, embedding_model=get_embedder().model_name
    )
    out: list[CorpusChunk] = []
    for r in rows:
        meta = read_structure(r.chunk_metadata)
        out.append(
            CorpusChunk(
                source_uri=source_uri,
                idx=int(r.chunk_idx),
                text=str(r.content or ""),
                section_path=meta.section_path,
                revision=meta.revision,
                locator=meta.locator,
            )
        )
    return out
