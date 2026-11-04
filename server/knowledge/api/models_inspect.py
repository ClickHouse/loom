"""Wire models for `knowledge.inspect`: what a namespace's documents are, which of their
versions search serves, and what was extracted from them, read from what is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.identity.api.models_findings import FindingContract, ReportCoverage

InspectSeverity = Literal["info", "warning"]


class KnowledgeInspectInput(BaseModel):
    """Report a namespace's documents, read-only.

    Counts by format, source and revision; the newest live documents with the chunks
    search serves and the decisions recorded from them; findings on the chunks; and the
    namespace audit's findings on memories shaped like documents. It reads what is
    stored and never parses a document. A section that cannot be read is named in
    `skipped` and its fields are null or empty.
    """

    org: str
    namespace: str
    # Newest live documents listed with their chunks and decisions, and the most sample URIs
    # per finding, 0 to 100 (default 20).
    sample_limit: int = Field(default=20, ge=0, le=100)


class DocumentCount(BaseModel):
    # The value counted: the row's format (knowledge.ingest sets it), else the format a
    # source sync converted it from, else its content type or file extension; or a
    # source_uri scheme. "(none)" when the document names none.
    name: str
    # Live documents with that value.
    live: int
    # Deleted documents (knowledge.delete) with that value.
    deleted: int


class DocumentTotals(BaseModel):
    """The namespace's documents, one row each (its current version)."""

    # Live documents; null when the read failed.
    live: int | None = None
    # Deleted documents, whose row is kept with `superseded_at` set; null when the read
    # failed.
    deleted: int | None = None
    # The newest `ingested_at` among live documents.
    newest_ingested_at: datetime | None = None
    # By the format the row records (knowledge.ingest sets it), else the format a source
    # sync converted it from (transcript, markdown, slides, or the extractor's pdf or
    # pptx), else the stored content type, else the file extension of the original
    # filename or the source_uri, most live first.
    by_format: list[DocumentCount] = Field(default_factory=list)
    # By the source_uri's scheme (`https`, `gdrive`, ...), most live first.
    by_source_scheme: list[DocumentCount] = Field(default_factory=list)
    # Published revisions before each document's current one, from the row's `revision`
    # (1, 2, ...); null when the read failed.
    earlier_revisions: int | None = None
    # Documents whose row no write numbered (`revision` 0), whose earlier revisions are
    # not counted; null when the read failed.
    unnumbered: int | None = None
    # Chunk versions retired, once per document and version: the revisions a later write
    # or a delete replaced, and every knowledge.ingest attempt that inserted chunks and
    # never published (failed, cancelled, or beaten by a later write), which no revision
    # numbers. Null when the read failed.
    retired_versions: int | None = None


class ChunkModelCount(BaseModel):
    # The embedding model the chunks were written under.
    model: str
    # Live chunks under that model.
    live: int
    # Those that carry a vector.
    embedded: int


class ChunkTotals(BaseModel):
    """The namespace's live chunks (of live versions)."""

    # The embedding model this server embeds queries with; knowledge.search reads only
    # the chunks written under it. Empty when the embedder could not be built.
    embedder_model: str = ""
    # Live chunks under any model; null when the read failed.
    live: int | None = None
    # Live chunks under `embedder_model` that carry a vector: what search can return.
    searchable: int | None = None
    # Live chunks with no vector, under any model.
    unembedded: int | None = None
    # Live chunks per embedding model, most first.
    by_model: list[ChunkModelCount] = Field(default_factory=list)


class DocumentChunks(BaseModel):
    """One document's live chunks."""

    # Live chunks under any model.
    live: int
    # Those under the server's embedding model that carry a vector.
    searchable: int
    # Those with no vector.
    unembedded: int
    # Those under another embedding model, which search does not read.
    other_model: int
    # Distinct document versions among the live chunks under the server's model; more
    # than one means search serves two versions of the document.
    versions: int
    # Characters per live chunk: smallest, median, largest.
    min_chars: int
    median_chars: int
    max_chars: int
    # The embedding models the live chunks were written under.
    models: list[str] = Field(default_factory=list)


class DecisionStage(BaseModel):
    """The decision rows of one stage."""

    # Decision rows at this stage.
    total: int
    # Pinned, or attested in their current content: someone stood behind the row as
    # written. For a proposed row that is the proposal, not a choice.
    confirmed: int
    # The rest.
    unconfirmed: int
    # Tagged `source-changed`: the document no longer says what the row says.
    source_changed: int
    # With a recorded outcome (decision.outcome).
    outcome_known: int


class DocumentDecisions(BaseModel):
    """Decision rows citing one document, or every document, by stage.

    Live decision rows in this namespace whose `source_resource_id` cites a section of
    one document, or of any document of the namespace."""

    # Recorded as chosen (the default stage).
    decided: DecisionStage
    # Recorded as put forward and not yet chosen or built (tagged `decision:proposed`).
    proposed: DecisionStage


class InspectDocument(BaseModel):
    """One live document."""

    source_uri: str
    title: str
    # The format the row records, else the format a source sync converted it from, else
    # the stored content type, else the file extension; "(none)" when none is known.
    format: str
    # The source_uri's scheme; "(none)" when it has none.
    source_scheme: str
    # The document row's version (knowledge.get serves it); a metadata edit raises it
    # without new chunks.
    version: int
    # Its published revision (1, 2, ...; 0 on a row no write numbered).
    revision: int
    # The knowledge.ingest job that published it; empty for a text write
    # (knowledge.add, upsert). Re-cut an ingested document with knowledge.ingest: a text
    # write turns it into a text document.
    ingestion_id: str = ""
    ingested_at: datetime
    # Chunk versions of this document that were retired, failed or cancelled ingest
    # attempts included; null when the read failed.
    retired_versions: int | None = None
    # Its live chunks; null when the read failed.
    chunks: DocumentChunks | None = None
    # The decision rows citing it; null when the read failed or was not made.
    decisions: DocumentDecisions | None = None


class DecisionTotals(BaseModel):
    """Decision rows that cite the namespace's documents."""

    # Which rows are counted, in one sentence.
    definition: str
    # Across every document of the namespace; null when the read failed or was not made.
    all_documents: DocumentDecisions | None = None


class DocumentFinding(FindingContract):
    """A finding on the namespace's documents and chunks.

    `kind` is `system_state` for the server's own state (chunks missing, unembedded, or
    under another model) and `usage_heuristic` for a pattern in how the documents were
    written."""

    code: str
    severity: InspectSeverity
    # Live documents that match.
    count: int
    # count / population (live documents).
    share: float = Field(ge=0.0, le=1.0)
    # The source_uri of documents that match, at most `sample_limit`.
    samples: list[str] = Field(default_factory=list)
    # Of `count`, the documents knowledge.ingest published, whose remedy is
    # knowledge.ingest, and their source_uri (at most `sample_limit`).
    ingested: int = 0
    ingested_samples: list[str] = Field(default_factory=list)
    detail: str
    remedy: str


class MemoryFindingRef(BaseModel):
    """A namespace.audit finding on memory rows shaped like documents.

    Cited by code and count; namespace.audit returns its samples and evidence."""

    code: str
    kind: str
    severity: str
    rule_version: int
    # Always `namespace.audit`.
    source_op: str
    # Rows that match.
    count: int
    share: float
    detail: str
    remedy: str


class KnowledgeInspectOutput(BaseModel):
    org: str
    namespace: str
    # When the report started (UTC).
    computed_at: datetime
    # Live and deleted documents by format and source scheme, and retired versions.
    documents: DocumentTotals
    # Live chunks by embedding model, and how many knowledge.search can return.
    chunks: ChunkTotals
    # Decision rows citing a document of the namespace, by stage.
    decisions: DecisionTotals
    # The newest live documents, at most `sample_limit`, newest first.
    listed: list[InspectDocument] = Field(default_factory=list)
    # True when `listed` holds every live document and the count was read.
    listed_complete: bool = False
    # One per check that ran over the documents and chunks; a clean check is an `info`
    # finding with count 0.
    findings: list[DocumentFinding] = Field(default_factory=list)
    # Which document checks ran and which were skipped.
    coverage: ReportCoverage
    # namespace.audit's `oversized_values` and `episodic_blobs`, the memory rows that
    # may belong in documents; mentor.brief reports the same shape per agent as
    # `documents_as_memories`.
    memory_findings: list[MemoryFindingRef] = Field(default_factory=list)
    # Sections not read, as "section: reason"; their fields are null or empty, not zero.
    skipped: list[str] = Field(default_factory=list)
