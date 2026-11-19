"""knowledge.list / get / outline: reads of a namespace's documents (a list also reads
several), and `document_chunks`, a document's served chunks in order (knowledge.synthesize
and `loom decisions extract` read through it)."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from server.core.cursors import (
    decode_bound_cursor,
    decode_cursor,
    encode_bound_cursor,
    encode_cursor,
)
from server.core.datetimes import to_naive_utc_second
from server.core.diagnostics import private_work
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
from server.knowledge.app import pg_serving, source_publication
from server.knowledge.app.readable import readable_namespaces
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.app.source_listing import Position
from server.knowledge.domain.chunk_metadata import read_structure
from server.knowledge.domain.chunking import Chunk
from server.knowledge.domain.document_access import public_tags
from server.operations.read_projection import resolve as _resolve_projection

if TYPE_CHECKING:
    from server.knowledge.store.documents import DocumentListRow, DocumentRow
    from server.memory.domain.corpus_synthesis import CorpusChunk

_LIST_ALWAYS = frozenset({"namespace", "source_uri"})


async def document_tally(org: str, namespace: str) -> tuple[int, datetime | None]:
    """How many current documents the namespace holds, and the newest ingest."""
    from server.knowledge.store import documents as documents_repo

    return await documents_repo.current_document_tally(org, namespace)


async def document_counts(org: str, namespaces: list[str]) -> dict[str, int]:
    """Current document metadata counts for namespaces the caller already authorized."""
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo

    with own_write_reads():
        return await documents_repo.current_document_counts(org, namespaces)


# ============================================================
# knowledge.list — paginated curation listing
# ============================================================


@dataclass(frozen=True)
class ListRequest:
    input: KnowledgeListInput
    namespaces: tuple[str, ...]
    cursor: Position | None


async def resolve_list(input: KnowledgeListInput) -> ListRequest:
    """Resolve authorized namespaces and the bound cursor before composing read plans."""
    input = input.model_copy(deep=True)
    namespaces = await readable_namespaces(input.org, input.namespace, input.namespaces)
    cursor = _list_position(input.cursor, input.org, namespaces) if input.cursor else None
    return ListRequest(input, tuple(namespaces), cursor)


@dataclass(frozen=True)
class _ListRenderer:
    request: ListRequest

    def render(
        self, ordinary: list[DocumentListRow], mirrored: list[DocumentListRow]
    ) -> KnowledgeListOutput:
        return _list_output(self.selected(ordinary, mirrored), self.request)

    def selected(
        self, ordinary: list[DocumentListRow], mirrored: list[DocumentListRow]
    ) -> list[DocumentListRow]:
        raw_rows = list(ordinary)
        if mirrored:
            families = {(row["namespace"], row["source_uri"]) for row in ordinary}
            raw_rows.extend(
                row for row in mirrored if (row["namespace"], row["source_uri"]) not in families
            )
            raw_rows.sort(
                key=lambda row: (
                    to_naive_utc_second(row["ingested_at"]),
                    row["namespace"],
                    row["source_uri"],
                ),
                reverse=True,
            )
            raw_rows = raw_rows[: self.request.input.limit + 1]
        return raw_rows


@dataclass
class CheckedList:
    output: KnowledgeListOutput
    proofs: ListProofs | None
    marked: bool
    _renderer: _ListRenderer
    _ordinary: list[DocumentListRow]
    lost: bool = False
    _failed: bool = field(default=False, init=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def _fallback(self) -> KnowledgeListOutput:
        if self._failed:
            raise LostCorpus()
        if self.proofs is not None:
            source_publication.discard(self.proofs)
            self.proofs, self.lost = None, True
            try:
                self.output = self._renderer.render(self._ordinary, [])
            except BaseException:
                self._failed = True
                raise
        return self.output

    async def ordinary(self) -> KnowledgeListOutput:
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                return self._fallback()

    async def current(self) -> KnowledgeListOutput:
        """Return the renewed page or one complete ordinary page with its own cursor."""
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                if self._failed:
                    raise LostCorpus()
                if self.proofs is not None:
                    try:
                        await self.proofs.check()
                    except LostCorpus:
                        return self._fallback()
                return self.output


async def handle_knowledge_list(input: KnowledgeListInput) -> KnowledgeListOutput:
    checked = await checked_knowledge_list(input)
    source_publication.retain(checked.proofs)
    return checked.output


async def checked_knowledge_list(input: KnowledgeListInput) -> CheckedList:
    from server.knowledge.app import source_access

    request = await resolve_list(input)
    plan = await source_access.prepare(request.input.org, list(request.namespaces))
    return await run_checked_list(request, plan)


async def run_checked_list(request: ListRequest, plan: ReadPlan) -> CheckedList:
    from server.core.ch import own_write_reads
    from server.knowledge.app import source_listing
    from server.knowledge.store import documents as documents_repo

    input = request.input
    if input.org != plan.org:
        raise ValueError("document read scope changed")
    with private_work() if plan.marked else nullcontext():
        with own_write_reads():  # a document added a call ago
            ordinary = await documents_repo.list_with_chunk_counts(
                input.org,
                list(request.namespaces),
                include_superseded=input.include_superseded,
                tag_filter=list_tag_filter(input.tags, every=input.tags_every),
                uri_prefixes=list(input.uri_prefix) if input.uri_prefix else None,
                cursor=request.cursor,
                limit=input.limit + 1,
            )
        proofs = None
        mirrored = []
        if plan.marked:
            listing = await source_listing.collect(
                plan, input, namespaces=list(request.namespaces), cursor=request.cursor
            )
            mirrored = listing.rows
            proofs = listing.proofs if listing.proofs.mirrored else None
        renderer = _ListRenderer(request)
        if proofs is not None:
            mirrored_rows = {id(row) for row in mirrored}
            proofs = proofs.selected(
                {
                    (row["namespace"], row["source_uri"])
                    for row in renderer.selected(ordinary, mirrored)
                    if id(row) in mirrored_rows
                }
            )
            if not proofs.mirrored:
                proofs = None
        output = renderer.render(ordinary, mirrored)
        lost = False
        if proofs is not None:
            try:
                await proofs.check()
            except LostCorpus:
                proofs, lost = None, True
                output = renderer.render(ordinary, [])
        return CheckedList(output, proofs, plan.marked, renderer, ordinary, lost=lost)


def _list_output(raw_rows: list[DocumentListRow], request: ListRequest) -> KnowledgeListOutput:
    input = request.input
    has_more = len(raw_rows) > input.limit
    page = raw_rows[: input.limit]

    rows = [
        _KnowledgeListEntry(
            namespace=r["namespace"],
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
        _list_cursor(page[-1], input.org, list(request.namespaces)) if rows and has_more else None
    )
    # The caller's field whitelist, resolved once.
    keep = _resolve_projection(_KnowledgeListEntry, input.fields, always=_LIST_ALWAYS)
    if keep is not None:
        for e in rows:
            e._projection = keep
    return KnowledgeListOutput(rows=rows, next_cursor=next_cursor)


def _list_cursor(last: DocumentListRow, org: str, namespaces: list[str]) -> str:
    """Position after `last`: the plain `<ts>|<uri>` cursor for one namespace, else a bound one."""
    if len(namespaces) == 1:
        return encode_cursor(last["ingested_at"], last["source_uri"])
    return encode_bound_cursor(
        _cursor_scope(org, namespaces),
        last["ingested_at"],
        json.dumps([last["namespace"], last["source_uri"]]),
    )


def _list_position(cursor: str, org: str, namespaces: list[str]) -> tuple[datetime, str, str]:
    """`_list_cursor` read back, for the same namespaces."""
    if len(namespaces) == 1:
        at, uri = decode_cursor(cursor)
        return at, namespaces[0], uri
    at, extra = decode_bound_cursor(cursor, _cursor_scope(org, namespaces))
    namespace, uri = json.loads(extra)
    return at, namespace, uri


def _cursor_scope(org: str, namespaces: list[str]) -> list[str]:
    return ["knowledge.list", org, *sorted(namespaces)]


async def handle_knowledge_outline(input: KnowledgeOutlineInput) -> KnowledgeOutlineOutput:
    """A document's current admitted sections, including chunks awaiting embedding."""
    from server.core.ch import own_write_reads
    from server.knowledge.app import source_access, source_documents
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    plan = await source_access.prepare(input.org, [input.namespace])
    with own_write_reads():
        doc = await documents_repo.get_one(input.org, input.namespace, input.source_uri)
    if doc is not None:
        listed = await knowledge_repo.list_for_doc(
            input.org, input.namespace, input.source_uri, embedding_model=get_embedder().model_name
        )
        rows, _ = await pg_serving.keep_served(input.org, listed)
        chunks = [_outline_chunk(r.chunk_idx, r.content, r.chunk_metadata) for r in rows]
        return _outline_output(input, doc.title or "", chunks)
    if not plan.marked:
        return KnowledgeOutlineOutput(found=False, source_uri=input.source_uri)
    checked = await source_documents.get(plan, input.namespace, input.source_uri)
    if checked is None:
        return KnowledgeOutlineOutput(found=False, source_uri=input.source_uri)
    admitted = await source_documents.chunks(plan, checked, model=get_embedder().model_name)
    if admitted is None:
        return KnowledgeOutlineOutput(found=False, source_uri=input.source_uri)
    chunks = [
        _outline_chunk(
            int(item.admitted.candidate["chunk_idx"]),
            str(item.row["content"] or ""),
            str(item.row["chunk_metadata"] or "{}"),
        )
        for item in admitted
    ]
    from server.knowledge.app.source_search_proofs import SearchProofs

    output = _outline_output(input, checked.row.title or "", chunks)
    source_publication.retain_document(plan, checked)
    source_publication.retain(
        SearchProofs(
            plan,
            get_embedder().model_name,
            tuple(item.admitted for item in admitted),
            embedded_only=False,
        )
    )
    return output


def _outline_chunk(idx: int, content: str, metadata: str) -> Chunk:
    meta = read_structure(metadata)
    return Chunk(
        idx=idx,
        text=content,
        section_path=meta.section_path,
        section_depth=meta.section_depth,
        section_idx=meta.section_idx,
        char_start=meta.char_start,
    )


def _outline_output(
    input: KnowledgeOutlineInput, title: str, chunks: list[Chunk]
) -> KnowledgeOutlineOutput:
    from server.knowledge.domain.chunking import outline

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
        title=title,
        structured=bool(entries),
        chunk_count=len(chunks),
        sections=entries,
        section_content=section_content,
        section_chunk_idxs=section_idxs,
    )


async def handle_knowledge_get(input: KnowledgeGetInput) -> KnowledgeGetOutput:
    """Fetch an ordinary document or one current, admitted source copy."""
    from server.knowledge.app import source_access, source_documents

    plan = await source_access.prepare(input.org, [input.namespace])
    row = await _ordinary_get_row(input)
    if row is not None or not plan.marked:
        return _get_output(row, input.source_uri)
    checked = await source_documents.get(plan, input.namespace, input.source_uri)
    if checked is not None:
        source_publication.retain_document(plan, checked)
    return _get_output(
        checked.row if checked else None, input.source_uri, mirrored=checked is not None
    )


async def ordinary_get(input: KnowledgeGetInput) -> KnowledgeGetOutput:
    """Read an ordinary document for mining without preparing a mirror leg."""
    return _get_output(await _ordinary_get_row(input), input.source_uri)


async def _ordinary_get_row(input: KnowledgeGetInput) -> DocumentRow | None:
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo

    with own_write_reads():  # a get right after an add must see it
        return await documents_repo.get_one(
            input.org,
            input.namespace,
            input.source_uri,
            include_superseded=input.include_superseded,
        )


def _get_output(
    row: DocumentRow | None, source_uri: str, *, mirrored: bool = False
) -> KnowledgeGetOutput:
    if row is None:
        return KnowledgeGetOutput(found=False, source_uri=source_uri)
    try:
        metadata = json.loads(row.metadata) if row.metadata else {}
    except json.JSONDecodeError:
        metadata = {}
    return KnowledgeGetOutput(
        found=True,
        source_uri=source_uri,
        title=row.title,
        content=row.content,
        tags=public_tags(row.tags) if mirrored else row.tags,
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
    """The document's served chunks for the current embedding model, in document order, each
    with its section path, the revision it was written as and its locator. Empty for a
    document chunked under another model, until `loom rechunk`."""
    from server.knowledge.store import chunks as knowledge_repo
    from server.memory.domain.corpus_synthesis import CorpusChunk

    listed = await knowledge_repo.list_for_doc(
        org, namespace, source_uri, embedding_model=get_embedder().model_name
    )
    rows, _ = await pg_serving.keep_served(org, listed)  # as the outline's
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
