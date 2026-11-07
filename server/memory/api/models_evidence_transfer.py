"""memory.export_evidence / memory.import_evidence — the raw chunks and session
digests behind a namespace, as portable rows."""

from __future__ import annotations

from pydantic import BaseModel, Field

from server.memory.domain.okf_evidence import IMPORT_MAX_ITEMS


class EvidenceChunk(BaseModel):
    session_id: str
    chunk_index: int = 0
    content: str
    content_sha256: str = ""
    agent: str = ""
    # The end user the chunk was written under ('-' = none). An import keeps it
    # when the importing request names no user and its token is not pinned to one.
    user_id: str = "-"
    parent_memory_key: str = ""
    # memory_chunk_sources links: the memory keys this chunk is evidence for.
    memory_keys: list[str] = Field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    created_at: str = ""
    source_url: str = ""


class EvidenceDigest(BaseModel):
    session_id: str
    summary_text: str
    agent: str = "-"
    user_id: str = "-"
    key_facts: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    valid_at: str = ""
    created_at: str = ""


class EvidenceEdge(BaseModel):
    """One live `loom.operator_edges` row: a typed link the
    bundle writes as `relates_to` and the importer re-creates with memory.link."""

    from_key: str
    to_key: str
    relation: str
    confidence: float = 1.0
    explanation: str = ""
    created_at: str = ""


class MemoryExportEvidenceInput(BaseModel):
    org: str
    namespace: str
    # `edges` pages the namespace's live operator_edges.
    kind: str = Field(default="chunks", pattern="^(chunks|digests|edges)$")
    session_id: str = ""  # optional: one session only; "" or "-" = every session
    # Rows per page (1–2000); `has_more` says whether another page follows.
    limit: int = Field(default=500, ge=1, le=2000)
    cursor: str = ""  # opaque keyset cursor from a previous page
    # Also export the chunks and digests behind a 'high' row or whose text holds a high
    # shape (a credential, card number, SSN or stated PIN), and the chunks stored without
    # a vector (one may hold part of a key). Off: a bundle reaches a wider audience than
    # the namespace's grants.
    include_high: bool = False


class MemoryExportEvidenceOutput(BaseModel):
    # `kind: chunks`: live chunks ordered by session then chunk index, each with its links.
    chunks: list[EvidenceChunk] = Field(default_factory=list)
    # `kind: digests`: the latest live digest per session, ordered by session id.
    digests: list[EvidenceDigest] = Field(default_factory=list)
    # `kind: edges`: live typed links, ordered by `(from_key, to_key, relation)`.
    edges: list[EvidenceEdge] = Field(default_factory=list)
    # True when more rows exist past this page.
    has_more: bool = False
    # Pass as `cursor` for the next page; empty on the last page and always for digests.
    next_cursor: str = ""
    # Chunks or digests of this page left out as 'high' (see `include_high`).
    held_high: int = 0


class MemoryImportEvidenceInput(BaseModel):
    org: str
    namespace: str
    agent: str
    user_id: str = "-"
    # Chunks to re-embed and insert (≤256); empty-content or session-less ones are skipped,
    # one that reads as high is inserted without a vector, and a key cut between
    # two calls' chunks is not found.
    chunks: list[EvidenceChunk] = Field(default_factory=list, max_length=IMPORT_MAX_ITEMS)
    # Session digests to re-embed and insert (≤256); empty summaries are skipped, and one
    # whose text reads as high is inserted without a vector.
    digests: list[EvidenceDigest] = Field(default_factory=list, max_length=IMPORT_MAX_ITEMS)
    # Stamped into memory_chunks.source_url when the chunk carries none.
    source_trace: str = Field(default="okf-import", max_length=512)


class MemoryImportEvidenceOutput(BaseModel):
    # Rows inserted into `loom.memory_chunks`, those written without a vector included.
    chunks_written: int = 0
    # Chunk-to-memory-key provenance links inserted from the chunks' `memory_keys`.
    links_written: int = 0
    # Rows inserted into `loom.session_summaries`.
    digests_written: int = 0
    # The server embedder the imported rows were re-embedded with.
    embedding_model: str = ""
    # Vector length of the written embeddings; 0 when nothing was embedded (every item
    # read as high, or none was written).
    embedding_dim: int = 0
    # Chunks whose body did not hash to the content_sha256 they arrived with.
    edited: list[str] = Field(default_factory=list)
    # Items not written: empty, unembeddable, or refused by a blacklist pattern (Refuse).
    skipped: int = 0
    # Of the skipped, those the blacklist could not check: the request's values are too
    # long for its regexes; sent in a smaller request they are checked, unless one is
    # too long alone, which must be shortened.
    unchecked: int = 0
