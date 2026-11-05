"""Knowledge plane operations.

Chunking is configurable via `knowledge_chunk_size` /
`knowledge_chunk_stride` settings; short payloads are one chunk per doc,
sliding-window kicks in past the configured size threshold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from server.knowledge.api.models_query import (
    MAX_DATASETS,
    KnowledgeQueryOutput,
    QueryDataset,
)
from server.knowledge.domain.answer_plan import CALCULATION_SOURCE
from server.knowledge.domain.datasets import DatasetType
from server.knowledge.domain.documents import MAX_SEARCH_TOP_K
from server.memory.domain.rerank_names import RerankName
from server.operations.base import MCP_HIDDEN
from server.operations.read_projection import ProjectedRow

# ============================================================
# Models
# ============================================================


class KnowledgeAddInput(BaseModel):
    org: str
    namespace: str
    # The document's stable identifier in the namespace; `get`, `upsert` and `delete` name it.
    source_uri: str
    # The document's extracted text; it is split into chunks, and each chunk is embedded.
    content: str
    # Document title, stored on the document and in each chunk's context, which heads
    # the chunk's embedded text and is scored by the lexical leg.
    title: str = ""
    # Stored (with the title) in each chunk's context, so both search legs carry
    # doc-level context.
    description: str = ""
    # Labels stored on the document and on every chunk; search and list filter on them.
    tags: list[str] = Field(default_factory=list)
    # Free-form JSON stored with the document and on every chunk; `knowledge.get` returns it.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Original file bytes (base64), preserved alongside extracted text so
    # users can download exactly what they uploaded.
    original_bytes_b64: str | None = None
    # Name of the uploaded file; empty keeps the prior document's name when its blob carries over.
    original_filename: str = ""
    # Media type of `original_bytes_b64`; empty keeps the prior blob's type when it carries over.
    blob_content_type: str = ""
    # The agent writing the document, stored on it and on every chunk for
    # provenance; an end-user token's signed `agent` takes its place. Empty = '-'.
    agent: str = Field(default="", max_length=128)
    # The session the document was written in (a chat, an agent run): stored on
    # it and on every chunk, and `knowledge.search` filters on it. Empty = '-'.
    session_id: str = Field(default="", max_length=256)
    # What the content is, when it is not the writer's own: `derived` (an agent
    # wrote it from other content), `external` (fetched: a page, a tool's
    # result) or `imported`. The server decides every other class.
    source_class: str | None = None


class KnowledgeAddOutput(BaseModel):
    # Id of the document version this call wrote (a fresh UUID per write).
    doc_id: str
    # How many chunks the content was split into and embedded.
    chunks_created: int
    # Embedding model that produced the chunk vectors.
    embedding_model: str
    # Dimension of the chunk vectors.
    embedding_dim: int
    # Echo the JWT-bound namespace so agents cite it instead of guessing.
    namespace: str = ""
    # Echo of the input `source_uri`.
    source_uri: str = ""


class KnowledgeUpsertInput(KnowledgeAddInput):
    """Same fields as knowledge.add. Behavior: replaces existing chunks for
    the same (org, namespace, source_uri); old chunks are superseded by listing
    their ingestion in loom.chunk_supersessions, so searches return the latest
    content while the rows stay for audit and time-travel."""


class KnowledgeUpsertOutput(BaseModel):
    # Id of the document version this call wrote (a fresh UUID per write).
    doc_id: str
    # How many chunks the new content was split into and embedded.
    chunks_created: int
    # How many live chunks of the prior version were superseded; 0 on a first write.
    chunks_superseded: int
    # Embedding model that produced the chunk vectors.
    embedding_model: str
    # Dimension of the chunk vectors.
    embedding_dim: int
    # Content-hash dedup surfacing: warn-not-block — the upsert succeeds
    # either way; the caller decides whether to reuse the existing doc.
    duplicate_of_uri: str = ""
    # New chunks whose content hash already lives under a different `source_uri`.
    duplicate_chunk_count: int = 0
    # Echo the JWT-bound namespace so agents cite it instead of guessing.
    namespace: str = ""
    # Echo of the input `source_uri`.
    source_uri: str = ""


class _SearchHit(BaseModel):
    source_uri: str
    chunk_idx: int
    content: str
    tags: list[str]
    distance: float  # cosine distance; 0=identical, 2=opposite
    similarity: float  # 1 - distance (caller-friendly)
    namespace: str = ""  # source namespace; populated on cross-namespace search
    ingested_at: datetime | None = None  # when the document was written
    # Where in the document this chunk sits: the heading
    # breadcrumb as a list and as "A > B > C". Empty for unstructured text.
    section_path: list[str] = Field(default_factory=list)
    section: str = ""
    # Raw BM25 over the query tokens (0 when the lexical leg did not fire)
    # and the ranking score: fused reciprocal rank in hybrid mode,
    # similarity in semantic mode, BM25 in lexical mode.
    lexical_score: float = 0.0
    score: float = 0.0
    # Plane marker (search-quality telemetry keys cosine metrics on it).
    source: str = "knowledge"
    # narrow_spans=True: the sentence window of this chunk nearest the query,
    # its character offsets in `content`, and its own similarity.
    # Empty / 0 when narrowing was not requested or the chunk is one window.
    span: str = ""
    span_start: int = 0
    span_end: int = 0
    span_similarity: float = 0.0
    # Other chunks of the same section that ranked in the candidate set and
    # were folded into this one (the section is the unit; one hit per section leaves
    # room for a second section).
    siblings_folded: int = 0
    # The document's provenance: the agent and session that wrote it ('-' for
    # none) and its class (`asserted` for a writer's own content; `derived`,
    # `external` or `imported` as the writer declared; '' before it was kept).
    agent: str = "-"
    session_id: str = "-"
    source_class: str = ""
    # The document revision this chunk was written as and the document's format (`sql`,
    # `markdown`, ...; '' for text a caller extracted), as the chunk records them: a
    # citation names the revision it read. 0 and '' for a chunk written before they were
    # recorded.
    revision: int = 0
    format: str = ""
    # For a chunk of an extracted file: its blocks' type (`sql_statement`, `toml_table`,
    # `paragraph`, `mixed`, ...) and where it sits in the source (`line_start`,
    # `line_end`, a key path, `page_start`/`page_end` for a PDF's pages or a deck's
    # slides, `sheet` and `row_start`/`row_end` for a sheet's, a CSV or a Parquet
    # file's rows).
    # '' and None for text a caller extracted.
    block_type: str = ""
    locator: dict[str, Any] | None = None
    # The query's identifier-shaped terms this chunk defines or reads (a table, a TOML
    # key path), from the exact-key leg of a hybrid search; empty otherwise.
    keys_matched: list[str] = Field(default_factory=list)


class KnowledgeSearchInput(BaseModel):
    org: str
    namespace: str
    # Natural-language query; it is embedded for the vector leg and tokenized for BM25.
    query: str
    # Keep only chunks carrying at least one of these tags; empty = no tag filter.
    tags: list[str] = Field(default_factory=list)
    since_seconds: int | None = None  # optional recency filter
    # At most this many hits are returned (1-100).
    top_k: int = Field(default=5, ge=1, le=MAX_SEARCH_TOP_K)
    # "this" → input.namespace only; "all_mine" → also the caller's own
    # namespaces, up to `knowledge_search_max_namespaces`. Ignored when
    # `namespaces` names the set; unset for an end-user token means "this".
    namespace_scope: Literal["this", "all_mine"] = "all_mine"
    # Search `namespace` and exactly these (an agent's collection, `@me`)
    # instead of a fan-out; each must be one the caller may read, else the call
    # is refused (403). Empty = `namespace_scope` decides.
    namespaces: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=100
    )
    # hybrid: the vector leg, the BM25 lexical leg and the exact-key leg (the query's
    # identifiers looked up in an extracted file's chunk keys) fused by reciprocal rank;
    # semantic: vector only;
    # lexical: BM25 only.
    search_mode: Literal["hybrid", "semantic", "lexical"] = "hybrid"
    # Weight of the lexical leg in reciprocal-rank fusion (hybrid mode).
    # 1.0 = equal weight; lower it for queries whose tokens are ubiquitous
    # in the corpus (paths, boilerplate) where the lexical leg cannot
    # discriminate.
    lexical_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    # Needle reads: also return, per hit, the two-sentence
    # window of the chunk nearest the query as `span`, so a caller after one
    # fact reads a sentence instead of a 1,500-character section. One extra
    # batched embed call over the returned hits' windows; the chunk is kept.
    narrow_spans: bool = False
    # Keep one hit per (document, section): the best-ranked chunk represents
    # its section and reports how many siblings it folded. Chunks without a
    # section (headless documents) are never folded. False returns raw chunks.
    collapse_sections: bool = True
    # Directory scope: only chunks whose source_uri starts
    # with one of these prefixes. A subtree of a hierarchical corpus
    # (`docs/guides/`, `repo://x/src/`) becomes a primary-key range on
    # loom.chunks, so scoping is cheaper than the unscoped search, not a
    # post-filter. Empty = whole namespace.
    uri_prefix: list[str] = Field(default_factory=list, max_length=8)
    # Only chunks of documents written in one of these sessions (a chat's own
    # attachments, an agent run's output). Empty = every session.
    session_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=64
    )
    # Only chunks of these classes, e.g. ["asserted"] for what people and their
    # agents wrote as their own, without what an agent derived or fetched.
    # Empty = every class.
    source_classes: list[str] = Field(default_factory=list, max_length=8)
    # Only chunks of documents in these formats: an ingested file's `format` (`sql`,
    # `toml`, `markdown`, ...), or '' for every other document, a synced one included
    # whatever `knowledge.inspect` names it (`knowledge.get` returns null for those).
    # A chunk written before formats were recorded is ''. Empty = every format.
    formats: list[Annotated[str, Field(max_length=32)]] = Field(default_factory=list, max_length=16)
    # Only chunks of these block types (`sql_statement`, `toml_table`, `toml_key`,
    # `heading`, `paragraph`, `code`, `mixed`, ...). Empty = every type; text a caller
    # extracted has none, so any entry leaves it out.
    block_types: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=16
    )
    # Reranker for the fused head: "" = the deployment default
    # (LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT), "none" = off, "builtin:openai[:<model>]",
    # "builtin:cohere[:<model>]" or an endpoint name in LOOM_RERANK_ENDPOINTS_JSON (no URL).
    rerank: RerankName = ""

    @field_validator("source_classes")
    @classmethod
    def _known_classes(cls, v: list[str]) -> list[str]:
        from server.memory.domain.provenance import SOURCE_CLASSES

        unknown = sorted(set(v) - set(SOURCE_CLASSES))
        if unknown:
            raise ValueError(f"source_classes {unknown} are not among {sorted(SOURCE_CLASSES)}")
        return list(dict.fromkeys(v))

    @field_validator("uri_prefix")
    @classmethod
    def _clean_prefixes(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for p in v:
            p = (p or "").strip()
            if not p:
                raise ValueError("uri_prefix entries must be non-empty")
            if len(p) > 1024:
                raise ValueError("uri_prefix entries must be at most 1024 characters")
            if p not in out:
                out.append(p)
        return out


class KnowledgeSearchOutput(BaseModel):
    # Hits best first (empty when nothing matched): when `reranked_by` is set, the
    # reranked head in the reranker's order, then the rest by `score`; else by `score`
    # (then similarity).
    results: list[_SearchHit]
    # The reranker that ordered `results`; empty when no reranker ran, it failed, it ran
    # past its time limit, or it kept the fused order.
    reranked_by: str = ""
    # Embedding model the query was embedded with; only chunks of this model are searched.
    embedding_model: str
    # Namespaces of the caller an all_mine search did not read because it stops
    # at `memory_search_fanout_max_namespaces`; address one by name to search it.
    namespaces_skipped: list[str] = Field(default_factory=list)


class _KnowledgeBulkEntry(BaseModel):
    content: str
    source_uri: str
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBulkAddInput(BaseModel):
    """Batched ingestion: many documents in one CH round-trip.

    Same per-doc behavior as `knowledge.add` (no upsert / supersede).
    Chunking still applies per-doc per the configured policy.
    """

    org: str
    namespace: str
    # The documents to add (1-500), each chunked and embedded like a `knowledge.add`.
    entries: list[_KnowledgeBulkEntry] = Field(..., min_length=1, max_length=500)
    # The agent writing the documents, stored on each and on every chunk for
    # provenance; an end-user token's signed `agent` takes its place. Empty = '-'.
    agent: str = Field(default="", max_length=128)
    # The session the documents were written in (a chat, an agent run): stored on
    # each and on every chunk, and `knowledge.search` filters on it. Empty = '-'.
    session_id: str = Field(default="", max_length=256)
    # What the content is, when it is not the writer's own: `derived` (an agent
    # wrote it from other content), `external` (fetched: a page, a tool's
    # result) or `imported`. The server decides every other class.
    source_class: str | None = None


class _KnowledgeBulkEntryResult(BaseModel):
    source_uri: str
    doc_id: str
    chunks_created: int


class KnowledgeBulkAddOutput(BaseModel):
    # One result per entry, in input order: its `source_uri`, new `doc_id` and chunk count.
    written: list[_KnowledgeBulkEntryResult]
    # Embedding model that produced the chunk vectors.
    embedding_model: str
    # Dimension of the chunk vectors.
    embedding_dim: int


class KnowledgeGetInput(BaseModel):
    """Fetch a single document by source_uri.

    Returns the latest non-superseded version by default; set
    `include_superseded=True` to read an old (soft-deleted) row.
    """

    org: str
    namespace: str
    # The document to fetch.
    source_uri: str
    # Also return a soft-deleted or superseded version; False returns only the live one.
    include_superseded: bool = False


class KnowledgeOutlineInput(BaseModel):
    """A document's table of contents, as recorded on its chunks at ingest,
    and optionally one section's assembled text.

    The move a reader makes with a long document: look at the outline, pick
    the section, read that. `section_path` selects a section by its heading
    breadcrumb (as returned in `sections[].path`); `include_children` folds
    its subsections in. A document whose chunks carry no headings reports
    `structured=False` and an empty outline."""

    org: str
    namespace: str
    # The document to outline.
    source_uri: str
    # Heading breadcrumb of the section to return as `section_content`;
    # empty returns the outline only.
    section_path: list[str] = Field(default_factory=list)
    # When True, `section_content` also includes the section's subsections.
    include_children: bool = True


class _OutlineEntry(BaseModel):
    path: list[str]
    title: str
    depth: int
    chunk_idxs: list[int]
    chars: int


class KnowledgeOutlineOutput(BaseModel):
    # False when no live document has this `source_uri`; the rest is then empty.
    found: bool
    # Echo of the input `source_uri`.
    source_uri: str
    # Document title.
    title: str = ""
    # True when the chunks carry section headings; False means `sections` is empty.
    structured: bool = False
    # How many chunks the document has for the current embedding model.
    chunk_count: int = 0
    # The table of contents in document order: each heading's path, depth, chunks and size.
    sections: list[_OutlineEntry] = Field(default_factory=list)
    # When `section_path` was given: the section's text (its chunks in order)
    # and the chunk indices it came from. None when no chunk matched.
    section_content: str | None = None
    # Chunk indices `section_content` was assembled from; empty when none matched.
    section_chunk_idxs: list[int] = Field(default_factory=list)


class KnowledgeSynthesizeInput(BaseModel):
    """Complete a task over WHOLE documents, not a search slice:
    every active chunk of the named documents is read in order, in windows
    that respect section boundaries, each window is noted once, and the notes
    are folded into the answer. With datasets, the task is also answered by a
    calculation over all their rows (knowledge.query), proposed by the model
    from their schemas and checked and run by the server. For summaries and
    questions whose evidence is spread across a long text or a table."""

    org: str
    namespace: str
    # The documents to read in full (up to 50; this, `datasets` or both); unknown URIs
    # contribute nothing.
    source_uris: list[str] = Field(default_factory=list, max_length=50)
    # Datasets to calculate over (up to 4, as knowledge.query takes them); one plan is
    # proposed for the task, checked against their schemas and run over every row.
    datasets: list[QueryDataset] = Field(default_factory=list, max_length=MAX_DATASETS)
    # What to do with the documents and datasets: a question to answer or a summary to write.
    task: str = Field(min_length=1, max_length=4000)
    # Characters of document text per map call; the reduce folds notes into batches of the
    # same size, and a calculation's note is at most half of it.
    window_chars: int = Field(default=24000, ge=2000, le=120000)
    # Cap on the answer's length in characters; a longer answer is cut and ends with `…`.
    max_answer_chars: int = Field(default=8000, ge=200, le=40000)
    # Cap on map calls; a corpus needing more is answered from the first
    # `max_windows` windows and `truncated` is set.
    max_windows: int = Field(default=64, ge=1, le=512)

    @model_validator(mode="after")
    def _reads_something(self) -> KnowledgeSynthesizeInput:
        if not self.source_uris and not self.datasets:
            raise ValueError("name source_uris, datasets or both")
        # With a dataset, a document by that name would cite as the calculation does.
        if self.datasets and CALCULATION_SOURCE in self.source_uris:
            raise ValueError(f"{CALCULATION_SOURCE!r} names the calculation, not a document")
        aliases = [d.alias for d in self.datasets]
        if len(set(aliases)) != len(aliases):
            raise ValueError("each dataset needs an alias of its own")
        return self


class SynthesizeCalculation(KnowledgeQueryOutput):
    """The calculation run for the task: knowledge.query's result (its columns, rows,
    row ids, completeness and receipt) and how the answer cites it."""

    # How the answer cites it: `knowledge.query#1`.
    citation: str


class _CitedChunk(BaseModel):
    source_uri: str
    chunk_idx: int
    # The document revision the chunk was written as (0 for a chunk written before it was
    # recorded), and where the chunk sits in the source for an extracted file
    # (`line_start`, `line_end`, a key path, ...; None for text a caller extracted).
    revision: int = 0
    locator: dict[str, Any] | None = None


class KnowledgeSynthesizeOutput(BaseModel):
    # The task's answer, citing chunks as `[source#idx]` and the calculation as
    # `[knowledge.query#1]`; empty when it abstained.
    answer: str
    # How many windows were read (one LLM map call each).
    windows: int
    # How many chunks the read windows covered.
    chunks_read: int
    # How many of the named documents had chunks to read.
    documents: int
    # How many fold rounds merged the notes into the answer; 1 when they fit one call.
    reduce_rounds: int
    # Distinct `source#idx` (or `source#a-b`, or a list of them for a window spanning
    # documents) citations in the answer, and `knowledge.query#1`, in the order found; each
    # names chunks the passes read or the calculation run.
    citations: list[str] = Field(default_factory=list)
    # Each chunk the kept `citations` name, once, in the order first cited: its revision
    # and its locator, so a caller can open the passage the answer came from.
    cited_chunks: list[_CitedChunk] = Field(default_factory=list)
    # Citations the model wrote naming a chunk the passes did not read, or a calculation not
    # run; removed from `answer`.
    unsupported_citations: list[str] = Field(default_factory=list)
    # True when the documents needed more than `max_windows` windows and only the first were read.
    truncated: bool = False
    # True when there is no answer to give; `abstain_reason` says why.
    abstain: bool = False
    # Why it abstained: `budget_capped` (over the cost cap), `no_chunks` (nothing to read),
    # `needs_clarification`, `ambiguous`, `invalid_plan` (see `clarification`) or `empty_answer`.
    abstain_reason: str = ""
    # ambiguous: the question to ask; invalid_plan: why no plan held; needs_clarification:
    # the dataset to approve (knowledge.dataset_approve); empty otherwise.
    clarification: str = ""
    # The calculation run over the datasets, cited in `answer` as `[knowledge.query#1]`.
    calculations: list[SynthesizeCalculation] = Field(default_factory=list)
    # LLM that wrote the notes and the answer; empty when it abstained before any call.
    model: str = ""


class KnowledgeGetOutput(BaseModel):
    # False when no document matched; every other field except `source_uri` is then None.
    found: bool
    # The document's identifier (the input `source_uri` when not found).
    source_uri: str
    # Document title.
    title: str | None = None
    # The document's full extracted text.
    content: str | None = None
    # The document's tags.
    tags: list[str] | None = None
    # The document's metadata JSON; `{}` when it was empty or unreadable.
    metadata: dict[str, Any] | None = None
    # When this version of the document was written.
    ingested_at: datetime | None = None
    # True when the returned version is soft-deleted or superseded.
    superseded: bool | None = None
    # Id of the returned document version.
    doc_id: str | None = None
    # The source's revision (1, 2, ... per source_uri); 0 for a document no write numbered.
    revision: int | None = None
    # For a file Loom extracted (knowledge.ingest): the format it was extracted as.
    format: str | None = None
    # For an extracted file: the extractor's parser and version that read it.
    parser: str | None = None
    # For an extracted file: what was extracted (unit, total, extracted, failed, lost).
    coverage: dict[str, Any] | None = None


class KnowledgeListInput(BaseModel):
    """Paginated listing of documents in a namespace, for curation tools."""

    org: str
    namespace: str
    # Documents carrying any one of these tags, in any case.
    tags: list[str] = Field(default_factory=list)
    # Require every one of `tags` instead of any one; default off. Not in the MCP listing:
    # the dashboard's stacked tag chips set it.
    tags_every: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Directory scope: documents whose source_uri starts
    # with one of these prefixes; empty = whole namespace.
    uri_prefix: list[str] = Field(default_factory=list, max_length=8)
    # Also list soft-deleted and superseded documents (their `superseded` is True).
    include_superseded: bool = False
    # At most this many rows per page (1-500); `next_cursor` fetches the next page.
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None  # opaque keyset cursor
    # The output fields the caller reads (a whitelist
    # resolved against the row model, as memory.list's is); unset,
    # every field. `source_uri` always rides. The Library page names the
    # eleven fields its rows render and drops the 24 kB of content_preview.
    fields: list[str] | None = None


class _KnowledgeListEntry(ProjectedRow):
    source_uri: str
    title: str
    description: str = ""
    tags: list[str]
    ingested_at: datetime
    chunk_count: int
    superseded: bool
    # Original-file metadata; has_blob is True when an original binary
    # was preserved at upload time and can be downloaded.
    has_blob: bool = False
    blob_size: int = 0
    blob_content_type: str = ""
    original_filename: str = ""
    # First ~200 chars of extracted text, for list snippets.
    content_preview: str = ""


class KnowledgeListOutput(BaseModel):
    # Documents, newest `ingested_at` first.
    rows: list[_KnowledgeListEntry]
    # Pass as `cursor` for the next page; None when this is the last page.
    next_cursor: str | None = None


class KnowledgeDeleteInput(BaseModel):
    """Soft-delete a document by source_uri: tombstones all live chunks
    and version-bumps the documents row. Searches stop returning it
    immediately; hard delete follows the 365d TTL.

    For right-to-erasure, use the erasure plane — it cascades across
    external systems too.
    """

    org: str
    namespace: str
    # The document to soft-delete.
    source_uri: str


class KnowledgeDeleteOutput(BaseModel):
    deleted: bool  # true if any live chunks were tombstoned
    # How many live chunks were tombstoned; 0 when the document was already gone.
    chunks_tombstoned: int


class KnowledgeUpdateMetadataInput(BaseModel):
    """Edit doc-level metadata (title, description, tags). Leaves chunk
    embeddings and stored context alone unless reindex=True. To change content, use
    knowledge.upsert; to rename the URI, delete + re-add — source_uri is
    the stable identifier the agent cites and isn't editable.
    """

    org: str
    namespace: str
    # The live document to edit; an unknown or deleted one is a 404.
    source_uri: str
    # None means "leave unchanged" (sparse update); empty string is an
    # explicit clear.
    title: str | None = None
    # New description; None leaves it unchanged, empty string clears it.
    description: str | None = None
    # New tag list, replacing the old one; None leaves it unchanged, `[]` clears it.
    tags: list[str] | None = None
    # When True, rewrite every chunk with the new title + description in its
    # context (re-embedded, re-scored by the lexical leg), routed through the upsert
    # flow for identical supersession semantics (same content, fresh chunks). A
    # document ingested from a file is refused (422): ingest it again instead.
    reindex: bool = False


class KnowledgeUpdateMetadataOutput(BaseModel):
    # True when a new version was written; False when every field already matched.
    updated: bool
    # Echo the post-update state so the client doesn't need to refetch.
    title: str = ""
    # The document's description after the call.
    description: str = ""
    # The document's tags after the call.
    tags: list[str] = Field(default_factory=list)
    # Count of chunks re-embedded when reindex=True; 0 otherwise.
    chunks_reindexed: int = 0


class KnowledgeBulkDeleteInput(BaseModel):
    """Tombstone multiple documents by source_uri in one lightweight UPDATE.

    Symmetric with knowledge.bulk_add. All source_uris are tombstoned
    together; the per-doc result tracks chunks tombstoned per uri.
    """

    org: str
    namespace: str
    # The documents to soft-delete (1-500).
    source_uris: list[str] = Field(..., min_length=1, max_length=500)


class _KnowledgeBulkDeleteResult(BaseModel):
    source_uri: str
    chunks_tombstoned: int


class KnowledgeBulkDeleteOutput(BaseModel):
    # One result per input URI, in input order, with its tombstoned chunk count (0 if none).
    deleted: list[_KnowledgeBulkDeleteResult]
    # Sum of the chunks tombstoned across every URI.
    total_chunks_tombstoned: int


class KnowledgeReadsInput(BaseModel):
    """How often agents opened each document over the last `days` (`knowledge.get`
    ledger rows; dashboard, assistant and bench traffic left out). Opened, not
    found — what a search returned is not recorded for knowledge."""

    org: str
    namespace: str
    # Look-back window in days (1-30).
    days: int = Field(7, ge=1, le=30)
    # empty = every document read in the window
    source_uris: list[str] = Field(default_factory=list, max_length=200)


class KnowledgeReadsOutput(BaseModel):
    # The look-back window the counts cover, in days, after clamping to 1-30.
    window_days: int
    # [{source_uri, reads, agents, last_read_at}], most read first
    reads: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeDedupListInput(BaseModel):
    """List cross-URI doc duplicates and (optionally) chunk-overlap
    clusters. ADMIN-only."""

    org: str
    namespace: str
    # At most this many clusters of each kind (1-200).
    limit: int = Field(default=50, ge=1, le=200)
    # Also return chunk-level clusters (identical chunks under different URIs).
    include_chunk_clusters: bool = False


class _KnowledgeDocCluster(BaseModel):
    content_sha256: str
    doc_count: int
    docs: list[dict[str, object]] = Field(default_factory=list)


class _KnowledgeChunkCluster(BaseModel):
    content_sha256: str
    chunk_count: int
    uri_count: int
    samples: list[dict[str, object]] = Field(default_factory=list)


class KnowledgeDedupListOutput(BaseModel):
    # Groups of documents under different URIs whose content hash is identical.
    doc_clusters: list[_KnowledgeDocCluster] = Field(default_factory=list)
    # Groups of identical chunks across URIs; empty unless `include_chunk_clusters`.
    chunk_clusters: list[_KnowledgeChunkCluster] = Field(default_factory=list)


class KnowledgeDedupResolveInput(BaseModel):
    """Resolve one document duplicate pair.

    Decisions:
      keep_canonical — soft-supersede the duplicate doc + its chunks.
      swap           — soft-supersede the canonical instead.

    `confirm_duplicate_uri` must equal `duplicate_uri` (defense).
    """

    org: str
    namespace: str
    # The document of the pair that `keep_canonical` keeps.
    canonical_uri: str
    # The document of the pair that `keep_canonical` supersedes.
    duplicate_uri: str
    # `keep_canonical` supersedes `duplicate_uri`; `swap` supersedes `canonical_uri`.
    decision: Literal["keep_canonical", "swap"]
    # Must equal `duplicate_uri`, or the call fails with 400 and nothing changes.
    confirm_duplicate_uri: str = Field(..., min_length=1, max_length=2048)


class KnowledgeDedupResolveOutput(BaseModel):
    # Echo of the input `canonical_uri`.
    canonical_uri: str
    # Echo of the input `duplicate_uri`.
    duplicate_uri: str
    # Echo of the input `decision`.
    decision: str
    # The URI that was soft-superseded.
    superseded_uri: str = ""


class KnowledgeIngestOptions(BaseModel):
    # The file's format when its name does not say (`markdown`, `html`, `text`, `sql`,
    # `toml`, ...); it never overrides what the bytes are.
    format: str = ""
    # The text encoding when it is not UTF-8 and the file has no byte-order mark.
    encoding: str = ""
    # The SQL dialect (`clickhouse`, `postgres`, `mysql`, ...); empty detects it.
    sql_dialect: str = ""


class KnowledgeIngestInput(BaseModel):
    """Extract an uploaded file (the extractor service parses it, the server
    checks what it returns) and publish it as a document revision,
    asynchronously: the answer names the job, and `knowledge.ingest_status`
    follows it."""

    org: str
    namespace: str
    # The `upload_id` POST /v1/knowledge.upload returned for the file.
    upload_id: str
    # The document's stable identifier in the namespace; a later ingest of the same
    # source_uri publishes its next revision.
    source_uri: str
    # Document title; empty takes the upload's file name.
    title: str = ""
    # Prepended with the title to each chunk before embedding.
    description: str = ""
    # Labels stored on the document and every chunk.
    tags: list[str] = Field(default_factory=list)
    # Free-form JSON stored with the document and every chunk.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Publish only if the source's live revision is this one (0: no live document);
    # unset publishes over whatever is live.
    expected_revision: int | None = Field(default=None, ge=0)
    # Publish an extraction that lost part of the source (coverage incomplete),
    # marked `partial`; off fails it instead.
    allow_partial: bool = False
    # Overrides for what the file cannot say about itself.
    options: KnowledgeIngestOptions = Field(default_factory=KnowledgeIngestOptions)
    # The agent writing the document; an end-user token's signed agent takes its place.
    agent: str = Field(default="", max_length=128)
    # The session the document was written in. Empty = '-'.
    session_id: str = Field(default="", max_length=256)
    # `derived`, `external` or `imported` when the content is not the writer's own.
    source_class: str | None = None


class KnowledgeIngestOutput(BaseModel):
    # The job's id; `knowledge.ingest_status` and `knowledge.ingest_cancel` take it.
    ingestion_id: str
    # `pending` until a worker takes it.
    state: str
    source_uri: str
    namespace: str = ""


class KnowledgeIngestStatusInput(BaseModel):
    """A knowledge.ingest job's state, stage, coverage, failure and published
    revision."""

    org: str
    namespace: str
    # The `ingestion_id` knowledge.ingest returned.
    ingestion_id: str


class KnowledgeIngestStatusOutput(BaseModel):
    # False when no job of this id is in the namespace; every other field is then empty.
    found: bool
    # The job asked about.
    ingestion_id: str
    source_uri: str = ""
    # pending, processing, ready, partial, failed or cancelled.
    state: str = ""
    # Where a processing job is, or where it last ran: parse, embed or publish;
    # empty once it published.
    stage: str = ""
    # Runs of the job so far, a retry after a transient failure included.
    attempts: int = 0
    # Why it failed (`format_unavailable`, `encoding`, `incomplete_extraction`,
    # `revision_conflict`, `too_large`, `no_text`, `retries_exhausted`, ...), or
    # `retrying` while it waits for its next attempt; empty otherwise.
    error_code: str = ""
    # The failure in words, for a person; empty when it did not fail.
    error_message: str = ""
    # The format the file was extracted as, empty before extraction.
    format: str = ""
    # What the extraction covered (unit, total, extracted, failed, lost).
    coverage: dict[str, Any] | None = None
    # The extractor's warnings about the file.
    warnings: list[str] = Field(default_factory=list)
    # Chunks published, 0 until the job publishes.
    chunks: int = 0
    # The revision the job published, 0 until it does.
    revision: int = 0
    # The revision the job requires to be live when it publishes; None for any.
    expected_revision: int | None = None
    # The uploaded file's name.
    filename: str = ""
    # Whether the document is searchable from this job: ready (published), pending
    # (not yet), failed (it will not be) or cancelled.
    retrieval: str = ""
    # Whether knowledge.ingest_cancel was called for the job.
    cancel_requested: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeIngestCancelInput(BaseModel):
    org: str
    namespace: str
    # The `ingestion_id` knowledge.ingest returned.
    ingestion_id: str


class KnowledgeIngestCancelOutput(BaseModel):
    # False when no job of this id is in the namespace.
    found: bool
    # The job's state after the call: `cancelled` for a job that had not started,
    # its running state when the worker will stop it before publishing, or where it
    # already ended (a published revision stays published).
    state: str = ""
    # True when this call stopped the job, or asked the worker to.
    cancelled: bool = False


# ============================================================
# knowledge.dataset_get / knowledge.dataset_approve
# ============================================================


class DatasetColumnOut(BaseModel):
    # The column's name, from the table's header (a repeated one as `<name> (2)`).
    name: str
    # The type its values are held as: string, number, bool, date, datetime,
    # time or duration (seconds).
    type: str
    # What the extractor inferred: one of the types, or `mixed` when the values differ.
    inferred: str
    # False when every slot of its type was taken: its values are not stored.
    stored: bool


class DatasetClarificationOut(BaseModel):
    # mixed_types, generated_name, duplicate_name, header_like_data or past_capacity.
    code: str
    # The column it is about; empty when it is about the table.
    column: str
    # What was found, in words.
    detail: str


class DatasetRejectedOut(BaseModel):
    # The row's place among the table's rows, from 1.
    row: int
    # The column whose value did not fit.
    column: str
    # Why it did not fit its column's type.
    reason: str


class DatasetOut(BaseModel):
    # The dataset's id, the same in every revision of its source.
    dataset_id: str
    # The ingested source it was published from.
    source_uri: str
    # The sheet's name, or the CSV or Parquet file's.
    name: str
    # The XLSX sheet the table came from; empty for a CSV or Parquet file.
    sheet: str = ""
    # The source's first row the table came from: its header row (from 1), or for a
    # source whose names are no row of it (a Parquet file) its first row of data,
    # 0 when it has none.
    row_start: int = 0
    # The source's last row the table came from.
    row_end: int = 0
    # The source revision the dataset was published from.
    revision: int
    # The snapshot serving it; knowledge.dataset_approve names the one it approves.
    snapshot_id: str
    # inferred, needs_clarification or approved.
    schema_state: str
    # The columns in the table's order, each with the type its values are held as.
    columns: list[DatasetColumnOut]
    # What a person must answer before the schema is trusted; approving answers them.
    clarifications: list[DatasetClarificationOut] = Field(default_factory=list)
    # Rows stored.
    row_count: int
    # Rows left out because a value did not fit its column's type (a cell
    # error, a text in a number column).
    rejected_rows: int = 0
    # The first rejected rows, with why.
    rejected: list[DatasetRejectedOut] = Field(default_factory=list)
    # False when the extraction did not keep every value of the table: rows or
    # values past a budget, merges not spread, formulas saved without values.
    complete: bool = True
    # Who approved the schema (it carries to a later revision with the same
    # columns); empty until approved.
    approved_by: str = ""
    # When the schema was approved; None until approved.
    approved_at: datetime | None = None
    # When this revision's dataset was first written.
    created_at: datetime | None = None
    # The first `sample_rows` rows, each a map of column to value (a number
    # past 2^53 as text).
    rows: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeDatasetGetInput(BaseModel):
    """The typed datasets an ingested file's tables became: their schemas,
    what needs a person's answer, the rows rejected and, on request, the first
    rows."""

    org: str
    namespace: str
    # The ingested source whose datasets are read.
    source_uri: str
    # One dataset of the source; empty for all of them.
    dataset_id: str = ""
    # Rows to return of each dataset, from its first (of the first 20 datasets).
    sample_rows: int = Field(default=0, ge=0, le=50)


class KnowledgeDatasetGetOutput(BaseModel):
    # False when the source has no live document; `datasets` is then empty.
    found: bool
    # The live document's revision.
    revision: int = 0
    # The live revision's datasets, in the order of the file's tables.
    datasets: list[DatasetOut] = Field(default_factory=list)


class KnowledgeDatasetApproveInput(BaseModel):
    """Approve a dataset's schema, retyping the columns named: its rows are
    typed again from the stored extraction under a new snapshot. A later
    revision of the source with the same columns keeps the approval."""

    org: str
    namespace: str
    # The ingested source whose dataset is approved.
    source_uri: str
    # The dataset to approve, as knowledge.dataset_get names it.
    dataset_id: str = Field(min_length=1)
    # The snapshot the caller reviewed (knowledge.dataset_get); refused once another serves.
    snapshot_id: str
    # Column name to the type it is held as; the columns not named keep theirs.
    columns: dict[str, DatasetType] = Field(default_factory=dict, max_length=1024)


class KnowledgeDatasetApproveOutput(BaseModel):
    # False when the source or the dataset is not live; a later revision, or
    # another approval that retyped the dataset, since the read is 409
    # PRECONDITION_FAILED (read it again).
    found: bool
    # The dataset as approved; None when not found.
    dataset: DatasetOut | None = None
    # True when a retyped column made a new snapshot.
    retyped: bool = False
