"""Wire models for `knowledge.inspect`: what a namespace's documents are, which of their
versions search serves, and what was extracted from them, read from what is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.identity.api.models_findings import FindingContract, ReportCoverage

InspectSeverity = Literal["info", "warning"]
# Why an ingestion job's last attempt failed, from its error_code (and, for an outage, the
# class of its message; the message itself is not returned):
# extractor_unavailable: no extractor takes the format, or the one routed to it is
#   unreachable, slow past its timeout or failing (failed, or pending to retry), or
#   answered an artifact that breaks the contract (`invalid_artifact`);
# missing_access: the stored original could not be read (`original_gone`), or the
#   extractor refused the server's credential (401, 403, 407);
# bad_content: the extractor rejected the file, or the server refused what it made of it
#   (an incomplete extraction without allow_partial, no text, too many chunks);
# retrying: an open job whose last attempt hit another outage (a store or embedder), or a
#   retried job a worker is running again; retries_exhausted: failed after such an outage
#   used up knowledge_ingest_max_attempts, or after runs cut off before they finished;
# revision_conflict: a later write of the source landed first; cancelled (a pending job
#   with a cancel requested is cancelled already); internal: a server defect; "" when it
#   did not fail.
FailureClass = Literal[
    "",
    "extractor_unavailable",
    "missing_access",
    "bad_content",
    "retrying",
    "retries_exhausted",
    "revision_conflict",
    "cancelled",
    "internal",
]


class KnowledgeInspectInput(BaseModel):
    """Report a namespace's documents, read-only.

    Counts by format, source and revision; the newest live documents with the chunks
    search serves, the decisions recorded from them, what the extractor kept and their
    knowledge.ingest jobs; the namespace's jobs, among them sources with a job and no live
    document (a first ingest, or one after a delete, queued or failed); findings on the
    chunks, the jobs and the extractions; and the namespace audit's findings on memories
    shaped like documents. It reads what is stored and never parses a document. A section
    that cannot be read is named in `skipped` and its fields are null or empty.
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
    # sync converted it from (transcript, markdown, slides, or the extractor's pdf,
    # pptx or docx), else the stored content type, else the file extension of the
    # original filename or the source_uri, most live first.
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
    # What the extractor kept of the live revision; null for a text write (knowledge.add,
    # upsert, a source sync), which no extractor read.
    extraction: ExtractionCoverage | None = None
    # The knowledge.ingest jobs of its source_uri, among them a newer one still open or one
    # that failed while this revision stayed live; null when there is none or the read
    # failed (`skipped` says which).
    jobs: SourceJobs | None = None


class IngestJob(BaseModel):
    """One knowledge.ingest job, as knowledge.ingest_status reads it, without its message."""

    # Pass it to knowledge.ingest_status or knowledge.ingest_cancel.
    ingestion_id: str
    # pending, processing, ready, partial, failed or cancelled; a pending job with a cancel
    # requested reads cancelled, the state knowledge.ingest_status answers for it.
    state: str
    # parse, embed or publish: where a processing job is or last ran; empty once published.
    stage: str
    # Runs started, counted against knowledge_ingest_max_attempts.
    attempts: int
    # The job's error_code ("" when its last attempt did not fail; `retrying` on a pending
    # job an outage put back).
    error_code: str
    failure_class: FailureClass
    # When knowledge.ingest queued it.
    queued_at: datetime
    # Its row's last change, as knowledge.ingest_status serves it; a cancel of a pending job
    # is a row of its own and does not change it.
    updated_at: datetime
    # When it ended (ready, partial, failed or cancelled): its row's last change, which for
    # a pending job cancelled is before the cancel; null while open.
    ended_at: datetime | None = None
    # When a pending job's next attempt is due; null once it is not pending.
    next_attempt_at: datetime | None = None
    # Processing, and no worker has updated it within its lease: the worker stopped, and a
    # later pass runs it again from its first stage.
    stalled: bool = False


class SourceJobs(BaseModel):
    """The knowledge.ingest jobs of one source_uri the job rows still hold (30 days)."""

    source_uri: str
    # Jobs of the source.
    jobs: int
    # Of those, pending or processing.
    open_jobs: int
    # The oldest open job, the one a worker runs next or is running ("" when none is
    # open): pass it to knowledge.ingest_status or knowledge.ingest_cancel.
    open_ingestion_id: str = ""
    # The newest job of the source.
    latest: IngestJob
    # The open job when one is (the source waits on it), else the newest: the job the
    # findings judge, unless `replaced`.
    current: IngestJob
    # No job is open and the source's document row was written or deleted after the newest
    # ended (a text write or a delete, which write no job row): that job's outcome no longer
    # says what search serves, and no finding counts the source. A metadata edit without
    # reindex keeps the row's ingested_at and replaces nothing; a reindex rewrites the
    # document and counts as a write.
    replaced: bool = False


class ExtractionCoverage(BaseModel):
    """What the extractor reported for a published revision, from its artifact's coverage."""

    # What the format is made of: lines, statements, keys, chars, pages, ...
    unit: str
    # Units in the source, and units extracted.
    total: int
    extracted: int
    # Units the parser could not extract.
    failed: int
    # Capabilities the artifact cannot serve for this source (sql_structure, exact_keys,
    # section_path, ...).
    lost: list[str] = Field(default_factory=list)
    # Every unit extracted, none failed and nothing lost.
    complete: bool


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


class IngestFinding(FindingContract):
    """A finding on the namespace's knowledge.ingest jobs or on what its extractions kept.

    `kind` is `system_state` for the deployment's state (a backlog, an extractor that is
    not there, access it lacks, a job that failed on the server's side); a file the
    extractor rejected is a `contract_violation` and a published revision that lost
    structure a `usage_heuristic`."""

    code: str
    severity: InspectSeverity
    # Sources (source_uri) that match.
    count: int
    # count / population: sources with a job for the job findings, live documents with an
    # extraction for `extraction_incomplete`.
    share: float = Field(ge=0.0, le=1.0)
    # The source_uri of sources that match, at most `sample_limit`.
    samples: list[str] = Field(default_factory=list)
    # The job each sample names, in the same order (the open one, else the newest): pass
    # it to knowledge.ingest_status. Empty for `extraction_incomplete`.
    ingestion_ids: list[str] = Field(default_factory=list)
    # Of `count`, sources with a job and no live document (a first ingest, or one after a
    # delete, queued or failed).
    without_document: int = 0
    detail: str
    remedy: str


class IngestionTotals(BaseModel):
    """The namespace's knowledge.ingest jobs the job rows still hold (30 days)."""

    # Sources with a job; null when the read failed.
    sources: int | None = None
    # Of those, sources with no live document, not counting ones `replaced`.
    without_document: int | None = None
    # Sources whose document row was written or deleted after their newest job ended, none
    # open: no finding counts them.
    replaced: int | None = None
    # Jobs by state, over every job.
    pending: int | None = None
    processing: int | None = None
    # Processing jobs no worker has updated within their lease.
    stalled: int | None = None
    ready: int | None = None
    partial: int | None = None
    failed: int | None = None
    cancelled: int | None = None
    # When the oldest open job was queued; null when none is open.
    oldest_open_queued_at: datetime | None = None
    # This server's settings: the ingest worker runs (LOOM_KNOWLEDGE_INGEST_ENABLED), and an
    # extractor is configured (LOOM_DOCUMENT_EXTRACTOR_URL or LOOM_DOCUMENT_EXTRACTORS_JSON).
    # Another replica may be set otherwise; the jobs say what happened.
    worker_enabled: bool
    # Null when the extractor settings could not be read (`skipped` says why).
    extractor_configured: bool | None = None
    # Sources with no live document and not `replaced`, newest job first, at most
    # `sample_limit`.
    without_document_listed: list[SourceJobs] = Field(default_factory=list)


class ExtractionTotals(BaseModel):
    """What the extractor kept of the live documents knowledge.ingest published."""

    # Live documents with an extraction's coverage; null when the read failed.
    documents: int | None = None
    # Of those, the ones not complete.
    incomplete: int | None = None
    # Per capability `coverage.lost` names, the live documents that lost it, most first.
    lost: list[DocumentCount] = Field(default_factory=list)


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
    # knowledge.ingest jobs by state, and the sources with a job and no live document (a
    # first ingest, or one after a delete, queued or failed).
    ingestions: IngestionTotals
    # What the extractor kept of the live ingested documents.
    extraction: ExtractionTotals
    # One per ingestion and extraction check that ran: a backlog, an extractor unavailable,
    # missing access, content the extractor rejected, jobs that failed on the server's side,
    # extractions that lost structure. A clean check is an `info` finding with count 0.
    ingest_findings: list[IngestFinding] = Field(default_factory=list)
    # namespace.audit's `oversized_values` and `episodic_blobs`, the memory rows that
    # may belong in documents; mentor.brief reports the same shape per agent as
    # `documents_as_memories`.
    memory_findings: list[MemoryFindingRef] = Field(default_factory=list)
    # Sections not read, as "section: reason"; their fields are null or empty, not zero.
    skipped: list[str] = Field(default_factory=list)
