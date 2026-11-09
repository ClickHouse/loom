"""One ingestion job: the original's bytes to a published document revision.

Stages, each recorded on the job row so `knowledge.ingest_status` shows where
it is:

1. **parse**: read the original and send its bytes to the extractor service its
   format is routed to (`server/knowledge/app/extractors.py`), which returns the
   artifact the server checks as untrusted input; store the artifact JSON as an
   original of its own (content-addressed, like the upload). The server parses
   nothing itself;
2. **embed**: cut structural chunks, reuse the stored vector of every chunk
   whose embedding input this namespace already embedded under the same model,
   embed the rest in one batch;
3. **publish**: decide the revision (and check the expected one), record the
   new chunk version (`doc_version`) on the job and insert its chunks, each
   naming that revision; then, under the source's claim (which other ingests
   of the source, a dataset approval, a metadata edit of an ingested document,
   a restore and a delete's retirement of a deleted source's leftovers take;
   every other write does not), check a cancel and refuse the
   publish when the source changed since the revision was decided, write the
   document row (the switch: revision, format, artifact, parser, coverage) and
   retire the chunks and the dataset snapshots of earlier versions. A table of
   the artifact becomes a typed dataset (`server/knowledge/app/datasets.py`),
   its rows and catalog row written with the chunks, before the claim. The
   claim covers those few statements only, however many chunks and rows the
   source has.

Every slow step runs before anything is published, so a job that fails leaves
the earlier revision serving. Chunks a publish inserted for a document row it
never wrote (an error, a cancel, a cancelled pass, a crash) are retired by the
job's retry, its failure or its next run; the document row names the job that
wrote it (`ingestion_id`), which is how a run tells its own publish. A job is
taken under a deployment-wide lease on its id, so two workers never run it at
once, and re-read under it, so a cancel (`loom.knowledge_ingest_cancels`, a
row of its own) or a finished run is seen. A transient failure (an extractor,
store or provider outage) puts the job back with a growing delay until
`knowledge_ingest_max_attempts`; anything else, a source the extractor refuses
included, fails the job with a typed code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core import pg
from server.core.ch import sequential_reads
from server.core.error_summary import error_summary
from server.core.errors import (
    BACKEND_NOT_NOW,
    BackendUnavailableError,
    EmbedTimeoutError,
    ValidationError,
)
from server.core.leases import LeaseLost
from server.core.pg import PostgresUnavailableError
from server.core.pg_lease import PublicationPending
from server.knowledge.app import datasets
from server.knowledge.app.extractors import (
    ExtractionFailed,
    ExtractorUnavailable,
    FormatUnavailable,
    extract,
)
from server.knowledge.domain.chunking import chunk_context, embedding_text
from server.knowledge.domain.extract.artifact import Artifact
from server.knowledge.domain.extract.structural import StructuralChunk, chunk_artifact
from server.knowledge.domain.ingest_states import OPEN_STATES
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import ingestions
from server.knowledge.store.documents import DocumentInsertRow, DocumentRow, OlderNotRetired
from server.knowledge.store.pg_documents import HeadMoved
from server.settings import settings

log = logging.getLogger(__name__)

# The lease a job runs under, and the claim a publish of one source runs under.
# A run that gives up on a document insert whose outcome is unknown leaves the job's lease to expire
# instead of releasing it (`Lease.let_expire`), so no worker takes the job before the insert has
# landed or stopped. The run's last `lease.held` check is inside the claimed block, and the lease
# was renewed within JOB_LEASE_S / 2 before it; the send comes within the block's bound of that
# check, so the lease expires at least JOB_LEASE_S / 2 minus the block's bound after the send,
# judged on the server's clock. That is past the insert's own bound: its send may start on the
# server up to `rmt_carry._TRANSIT_S` later, run for a claimed statement's server-side limit, and
# commit within `rmt_carry._RELEASE_MARGIN_S`. The rerun reads its outcome from the stored rows.
JOB_LEASE_S = 120.0  # not a setting: renewed while the job runs; it bounds a dead holder
_SOURCE_CLAIM_S = 60.0  # not a setting: bounds one publish's few statements
# The first retry waits this long, doubling per attempt.
_RETRY_BASE_S = 5  # not a setting: knowledge_ingest_max_attempts bounds the retries
# The longest wait between attempts, far inside the job row's 30-day TTL.
_RETRY_MAX_S = 3600  # not a setting: a longer outage is the attempts' to ride out

# A store that cannot answer now (down, past its deadline, out of slots), an
# embedder past its deadline, an extractor that cannot take the source now.
# A Postgres error at the document's commit that may have committed it
# (`outcome_unknown`) stops the run as an uncertain insert does, and the next one
# settles it; one that committed nothing (a lock timeout under an erasure) retries.
_TRANSIENT = (*BACKEND_NOT_NOW, EmbedTimeoutError, ExtractorUnavailable, PostgresUnavailableError)


class JobFailed(Exception):
    """The job cannot succeed as asked: `code` says why, for the status op."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _InsertUncertain(Exception):
    """The document row's insert failed transiently (a client-side timeout
    among them, or the source claim's deadline cutting it off): the row may
    still land. The run leaves the job's lease to expire and stops as a crash
    would, writing nothing more, so the job stays `processing` at its publish
    stage; once the lease has expired its next run settles it as it settles
    any publish that stopped midway. `cause` is the transient error."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _AfterRow(Exception):
    """This run's document row was written and a later step failed, for any
    reason (the retire of the earlier version's chunks, the claim's release):
    the publish stands, so the job ends published. `cause` is the error."""

    def __init__(self, revision: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.revision = revision
        self.cause = cause


class JobGone(Exception):
    """The job's row is gone: its namespace or user was erased while it ran.
    Nothing more is written for it, not even its own row."""


@dataclass(frozen=True)
class Parsed:
    artifact: Artifact
    artifact_sha256: str
    chunks: list[StructuralChunk]
    original_size: int  # the stored original's bytes, for the document row


def claim_source(org: str, namespace: str, source_uri: str) -> AbstractAsyncContextManager[bool]:
    """The source's claim, which an ingest's publish, a dataset approval, a metadata
    edit of an ingested document, a restore and a delete's retirement of a deleted
    source's leftovers take (text writes and a live document's delete do not):
    `async with` it yields whether it is held."""
    from server.core import leases

    return leases.holding(
        f"knowledge-source:{org}:{namespace}:{source_uri}", ttl_s=_SOURCE_CLAIM_S, wait_s=10
    )


def _author(job: dict[str, Any]) -> dict[str, str]:
    return {c: str(job[c]) for c in datasets_repo.AUTHOR}


async def _current(job: dict[str, Any]) -> dict[str, Any]:
    """The job's row as it is now; JobGone once an erasure has deleted it."""
    current = await ingestions.get_ingestion(job["org"], job["namespace"], job["ingestion_id"])
    if current is None:
        raise JobGone(job["ingestion_id"])
    return current


async def _save(job: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """A new version of the job row; only the worker that holds the job's lease
    writes them. The chunk version a publish recorded is kept: a caller's copy
    of the row may predate it. A job an erasure deleted is not written again
    (JobGone)."""
    current = await _current(job)
    fields.setdefault("doc_version", current["doc_version"])
    row = ingestions.ingestion_row(job, **fields)
    await ingestions.insert_ingestion(row)
    return row


async def _cancelled(job: dict[str, Any]) -> bool:
    return await ingestions.cancel_requested(job["org"], job["namespace"], job["ingestion_id"])


async def _own_publish(job: dict[str, Any]) -> int | None:
    """The revision this job published (an earlier run that stopped after its
    document row), whatever has been written to the source since; None when it
    published nothing."""
    return await documents_repo.revision_by_ingestion(
        job["org"], job["namespace"], job["source_uri"], job["ingestion_id"]
    )


async def _retire_unpublished(job: dict[str, Any]) -> None:
    """Retire the chunk version the job last recorded, for a document row it
    never wrote: search must not serve a revision that was not published. Its
    callers know the row was not written: a run that failed before its own
    insert returned, or one `take_and_run` found unsettled. Only this job's own
    chunk version is touched, under its lease; retiring it again is a no-op."""
    from server.knowledge.store import chunks as chunks_repo

    version = int((await _current(job))["doc_version"])
    if not version:
        return
    await chunks_repo.mark_doc_superseded(
        job["org"], job["namespace"], job["source_uri"], only_version=version
    )
    await datasets_repo.forget(job["org"], job["namespace"], [job["source_uri"]], only=version)


async def _end(job: dict[str, Any], state: str, code: str, message: str) -> str:
    """End a job that publishes nothing: failed or cancelled."""
    await _retire_unpublished(job)
    await _save(job, state=state, error_code=code, error_message=message[:2000])
    return state


async def _overtaken(job: dict[str, Any]) -> bool:
    """A run of this job stopped mid-publish (a cancelled pass, a crash: the job
    is still `processing` at the publish stage, which is saved with its chunk
    version, the version its document row takes), and a later write of the
    source has landed since (a text write, a metadata edit or a delete).
    Whether the job's row landed before that write cannot be told once a merge
    has folded the row away, so a rerun cannot publish without perhaps putting
    this job's older file over the later write. A job a retry put back to
    pending, or one stopped before it published, is not judged."""
    version = int(job["doc_version"])
    if job["state"] != "processing" or job["stage"] != ingestions.PUBLISH or not version:
        return False
    doc = await documents_repo.get_one(
        job["org"], job["namespace"], job["source_uri"], include_superseded=True
    )
    return doc is not None and doc.version > version


async def _finish_published(job: dict[str, Any], revision: int) -> str:
    """End a job whose document row landed but whose run did not finish (it
    stopped before its last save, or failed after the row): retire the earlier
    versions' chunks it may not have reached, and record the revision. Nothing
    is published again."""
    from server.knowledge.store import chunks as chunks_repo

    org, ns, uri = job["org"], job["namespace"], job["source_uri"]
    if int(job["doc_version"]):
        await chunks_repo.mark_doc_superseded(org, ns, uri, before_version=int(job["doc_version"]))
        await datasets_repo.forget(org, ns, [uri], before=int(job["doc_version"]))
    complete = json.loads(job["coverage"] or "{}").get("complete", True)
    state = "ready" if complete else "partial"
    await _save(
        job,
        state=state,
        stage="",
        revision=revision,
        chunks=await chunks_repo.count_version(org, ns, uri, int(job["doc_version"])),
        error_code="",
        error_message="",
    )
    return state


def render_text(artifact: Artifact) -> str:
    """The document text `knowledge.get` serves for a parsed source: its blocks
    in order, a heading as Markdown so the text keeps its structure."""
    parts = []
    for b in artifact.blocks:
        parts.append(f"{'#' * max(1, min(b.depth, 6))} {b.text}" if b.type == "heading" else b.text)
    return "\n\n".join(p for p in parts if p.strip())


async def _parse(job: dict[str, Any]) -> Parsed:
    from server.knowledge.store import blobs

    org, ns, sha = job["org"], job["namespace"], job["sha256"]
    stored = await blobs.locate(org, ns, sha)
    data = await blobs.read(org, ns, sha, stored=stored) if stored is not None else None
    if data is None:
        raise JobFailed("original_gone", "the uploaded original is no longer stored")
    opts = json.loads(job["options"] or "{}")
    # Options go to the extractor only when set: one that does not take an
    # option refuses it, even an empty one.
    passed = {k: str(opts[k]) for k in ("encoding", "sql_dialect") if opts.get(k)}
    try:
        artifact = await extract(
            data,
            name=job["filename"],
            media_type=job["content_type"],
            declared_format=str(opts.get("format") or ""),
            options=passed,
        )
    except ExtractionFailed as e:  # the source, not the service: retrying cannot help
        raise JobFailed(e.code, str(e)) from e
    except FormatUnavailable as e:  # the deployment's extractors, which retries do not change
        raise JobFailed("format_unavailable", str(e)) from e
    if not artifact.coverage.complete and not job["allow_partial"]:
        raise JobFailed(
            "incomplete_extraction",
            f"{artifact.coverage.extracted} of {artifact.coverage.total} {artifact.coverage.unit}"
            " extracted; ingest with allow_partial to publish it anyway",
        )
    size = settings.knowledge_chunk_size
    chunks = chunk_artifact(artifact, size=size, stride=min(settings.knowledge_chunk_stride, size))
    if not chunks:
        raise JobFailed("no_text", "the file holds no text to index")
    if len(chunks) > settings.knowledge_ingest_max_chunks:
        raise JobFailed(
            "too_large",
            f"{len(chunks)} chunks; this deployment publishes at most "
            f"{settings.knowledge_ingest_max_chunks} per source",
        )
    await _current(job)  # an erased job stores no extracted text
    sha = await blobs.put(
        job["org"], job["namespace"], artifact.to_json(), uploader=str(job["user_id"])
    )
    return Parsed(artifact, sha, chunks, len(data))


async def _vectors(job: dict[str, Any], inputs: list[str]) -> tuple[list[list[float]], int]:
    """A vector per embedding input, and how many were reused: an input this
    namespace already embedded under the live model keeps its stored vector."""
    from server.core.vectors import codes_state
    from server.embedding import get_embedder
    from server.knowledge.store import chunks as chunks_repo

    embedder = get_embedder()
    await codes_state.refuse_width("chunks", embedder.dim)
    shas = [hashlib.sha256(i.encode()).hexdigest() for i in inputs]
    stored = await chunks_repo.vectors_by_input(
        job["org"], job["namespace"], embedder.model_name, embedder.dim, sorted(set(shas))
    )
    missing = sorted({i for i, sha in zip(inputs, shas, strict=True) if sha not in stored})
    fresh = dict(zip(missing, await embedder.embed_many(missing), strict=True)) if missing else {}
    out = [stored[sha] if sha in stored else fresh[i] for i, sha in zip(inputs, shas, strict=True)]
    return out, len(inputs) - sum(1 for sha in shas if sha not in stored)


def _context(job: dict[str, Any], chunk: StructuralChunk) -> str:
    """A chunk's context, as `knowledge.add` gives its chunks: the document's
    title and description and the chunk's section breadcrumb, which both search
    legs read."""
    return chunk_context(job["title"], job["description"], chunk.breadcrumb)


def _revision_for(job: dict[str, Any], prior: DocumentRow | None) -> int:
    """The revision this publish takes over the source's latest row `prior`, or
    JobFailed("revision_conflict") when the job may not publish over it."""
    from server.knowledge.app.writes import next_revision

    current = prior if prior is not None and prior.superseded_at is None else None
    expected = int(job["expected_revision"])
    at = current.revision if current is not None else 0
    # 0 expects no live document; a live one written before revisions were
    # numbered (or by an older build) also reads revision 0.
    if expected >= 0 and (at != expected or (expected == 0 and current is not None)):
        now_at = f"revision {at}" if current is not None else "no live document"
        raise JobFailed("revision_conflict", f"the source has {now_at}, not {expected}")
    return next_revision(prior)


async def _check_unchanged(
    job: dict[str, Any], prior: DocumentRow | None, datasets_read: datasets.Published
) -> None:
    """Under the source's claim: JobFailed("revision_conflict") when the source is no
    longer as it was when this publish decided its revision; a retry when one of its
    datasets was approved since the publish read them (`datasets.check_unchanged`)."""
    latest = await documents_repo.get_one(
        job["org"], job["namespace"], job["source_uri"], include_superseded=True
    )
    # Any row written since the revision was decided (a text write or a live delete, which
    # take no claim, or a metadata edit or restore that took it first) changes the latest
    # version, so this publish's revision, which its chunks record, is stale. Its version
    # is past the one it read (`_publish`): with the source unchanged its row outranks
    # every earlier one in FINAL.
    if documents_repo.version_of(latest) != documents_repo.version_of(prior):
        raise JobFailed("revision_conflict", "the source changed while it was published")
    await datasets.check_unchanged(job, datasets_read)


async def _publish_datasets(
    job: dict[str, Any],
    parsed: Parsed,
    version: int,
    revision: int,
    prior: DocumentRow | None,
    lease: Any,
) -> datasets.Published:
    """The artifact's tables as datasets of the job's document version; an
    approved dataset of the live `prior` row carries its types."""
    live = prior if prior is not None and prior.superseded_at is None else None
    return await datasets.publish(
        job,
        parsed.artifact,
        doc_version=version,
        revision=revision,
        artifact_sha256=parsed.artifact_sha256,
        prior_version=live.version if live else 0,
        lease=lease,
        alive=lambda: _current(job),  # an erased job writes no more of its rows
    )


def _refuse_unstorable_keys(job: dict[str, Any]) -> None:
    """JobFailed("unstorable_text") when the document row could not be committed: its
    source URI or its writer's id holds a NUL character or a lone surrogate, which a
    Postgres key column cannot store (`documents.check_storable`). `knowledge.ingest`
    refuses such a job (422); this catches one an older build queued."""
    try:
        documents_repo.check_storable([{**job, **_author(job)}])
    except ValidationError as e:
        raise JobFailed(
            "unstorable_text",
            "the source URI or the writer's id holds a NUL character or a lone surrogate",
        ) from e


def _chunk_rows(
    job: dict[str, Any],
    parsed: Parsed,
    vectors: list[list[float]],
    *,
    version: int,
    revision: int,
    now: datetime,
) -> list[Any]:
    """The new version's chunk rows, embedded with the configured model."""
    from server.embedding import get_embedder
    from server.knowledge.store import chunks as chunks_repo

    embedder = get_embedder()
    return chunks_repo.build_chunk_rows(
        org=job["org"],
        namespace=job["namespace"],
        source_uri=job["source_uri"],
        title=job["title"],
        chunks=parsed.chunks,
        contexts=[_context(job, c) for c in parsed.chunks],
        embeddings=vectors,
        tags=list(job["tags"]),
        metadata=job["metadata"],
        doc_version=version,
        revision=revision,
        doc_format=parsed.artifact.format,
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        now=now,
        author=_author(job),
    )


async def _publish(
    job: dict[str, Any], parsed: Parsed, vectors: list[list[float]], lease: Any
) -> tuple[dict[str, Any], int]:
    """Insert the new version and switch the document to it; returns the job
    row as saved and the revision."""
    from server.knowledge.store import chunks as chunks_repo

    org, ns, uri = job["org"], job["namespace"], job["source_uri"]
    if await _cancelled(job):
        raise JobFailed("cancelled", "cancelled before it was published")
    await _retire_unpublished(job)  # an earlier run's half-done publish
    # The revision is decided before the chunks are written, since each chunk records
    # it; the claimed block refuses the publish if the source has changed since.
    prior = await documents_repo.get_one(org, ns, uri, include_superseded=True)
    revision = _revision_for(job, prior)
    now = datetime.now(UTC).replace(tzinfo=None)
    version = documents_repo.version_past(documents_repo.version_of(prior))
    _refuse_unstorable_keys(job)  # refused before any chunk is written
    rows = _chunk_rows(job, parsed, vectors, version=version, revision=revision, now=now)
    if not lease.held:  # another worker may have taken the job: it publishes, not this run
        raise LeaseLost(f"knowledge-ingest:{job['ingestion_id']}")
    # Recorded before the insert, so a run that stops after it is retired; the
    # stage goes with it, so the publish stage always names this version.
    job = await _save(job, stage=ingestions.PUBLISH, doc_version=version)
    await chunks_repo.insert_chunks(rows)
    datasets_read = await _publish_datasets(job, parsed, version, revision, prior, lease)
    # Set while the document row's insert has been sent and has not returned:
    # a transient failure then (the statement's own timeout, or the claim's
    # deadline cutting it off) may leave the row to land later.
    in_flight = published = False
    try:
        async with claim_source(org, ns, uri) as held:
            if not held:
                raise BackendUnavailableError(
                    "another write of this source holds it", operation="knowledge.ingest"
                )
            if await _cancelled(job):
                raise JobFailed("cancelled", "cancelled before it was published")
            await _check_unchanged(job, prior, datasets_read)
            if not lease.held:
                raise LeaseLost(f"knowledge-ingest:{job['ingestion_id']}")
            row = _document_row(job, parsed, version=version, revision=revision, now=now)
            await _current(job)  # an erased job writes no document row
            in_flight = True
            try:
                await _insert_document(row, prior)
            except OlderNotRetired:  # the row is in: the publish stands
                in_flight, published = False, True
                raise
            in_flight, published = False, True
            await datasets_repo.forget(org, ns, [uri], before=version)
    except asyncio.CancelledError:
        if in_flight:  # stopped (a pass deadline, a shutdown) with the insert in flight
            lease.let_expire()
        raise
    except Exception as e:
        if in_flight and isinstance(e, _TRANSIENT) and _may_have_landed(e):
            lease.let_expire()
            raise _InsertUncertain(e) from e
        if published:  # whatever failed after the row, the publish stands
            raise _AfterRow(revision, OlderNotRetired.unwrap(e)) from e
        raise
    return job, revision


def _document_row(
    job: dict[str, Any], parsed: Parsed, *, version: int, revision: int, now: datetime
) -> DocumentInsertRow:
    """The loom.documents row the job publishes: its extracted text, original and
    artifact, at `version` and `revision`."""
    return documents_repo.build_document_row(
        **_author(job),
        org=job["org"],
        namespace=job["namespace"],
        source_uri=job["source_uri"],
        title=job["title"],
        description=job["description"],
        content=render_text(parsed.artifact),
        tags=list(job["tags"]),
        metadata=job["metadata"],
        version=version,
        ingested_at=now,
        blob_path=job["sha256"],
        blob_size=parsed.original_size,
        blob_content_type=job["content_type"],
        original_filename=job["filename"],
        revision=revision,
        format=parsed.artifact.format,
        artifact_sha256=parsed.artifact_sha256,
        parser=f"{parsed.artifact.parser}/{parsed.artifact.parser_version}",
        coverage=json.dumps(parsed.artifact.coverage.to_dict(), sort_keys=True),
        ingestion_id=job["ingestion_id"],
    )


def _may_have_landed(e: Exception) -> bool:
    """Whether a transient failure of the document's insert may still have written it:
    a ClickHouse insert that failed may land later; a Postgres commit only when its
    answer was lost."""
    if isinstance(e, PostgresUnavailableError):
        return pg.may_have_committed(e)
    return True


async def _insert_document(row: DocumentInsertRow, prior: DocumentRow | None) -> None:
    """The document row, then the older versions' chunks retired (`retire_older`).
    OlderNotRetired when only the retirement failed, the row in; JobFailed when a write
    of the source committed since `prior` was read."""
    try:
        await documents_repo.insert_one(
            row, over=documents_repo.version_of(prior), retire_older=True, chunked=True
        )
    except HeadMoved as e:
        raise JobFailed("revision_conflict", "another write of the source committed first") from e
    except PublicationPending:
        pass  # committed: the delivery worker publishes the row and the retirement


async def _retry_or_fail(job: dict[str, Any], e: Exception) -> str:
    """After a transient failure: back to pending with a doubling delay, or
    failed once the attempts are spent."""
    attempts = int(job["attempts"])
    if attempts >= settings.knowledge_ingest_max_attempts:
        return await _end(job, "failed", "retries_exhausted", str(e))
    # Retired before it waits, so search does not serve them meanwhile.
    await _retire_unpublished(job)
    delay = min(_RETRY_BASE_S * 2 ** (attempts - 1), _RETRY_MAX_S)
    await _save(
        job,
        state="pending",
        error_code="retrying",
        error_message=str(e)[:2000],
        next_attempt_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=delay),
    )
    return "pending"


async def _run_again(job: dict[str, Any]) -> str:
    """Back to pending at once, the attempt not counted: the run stopped for
    another write (an approval), not for a failure."""
    await _retire_unpublished(job)
    await _save(
        job,
        state="pending",
        attempts=int(job["attempts"]) - 1,
        next_attempt_at=datetime.now(UTC).replace(tzinfo=None),
    )
    return "pending"


async def run(job: dict[str, Any], lease: Any) -> str:
    """Run one open job to its next state; returns that state. The caller
    holds the job's `lease`, renewed; the publish refuses once it is lost.
    A job whose attempts are spent fails before any work: a run that never
    finished (a pass cancelled at its deadline, a process that died) did not
    count its failure."""
    if int(job["attempts"]) >= settings.knowledge_ingest_max_attempts:
        return await _end(
            job, "failed", "retries_exhausted", "earlier runs stopped before they finished"
        )
    job = await _save(
        job, state="processing", stage=ingestions.PARSE, attempts=int(job["attempts"]) + 1
    )
    try:
        parsed = await _parse(job)
        job = await _save(
            job,
            stage=ingestions.EMBED,
            format=parsed.artifact.format,
            artifact_sha256=parsed.artifact_sha256,
            coverage=json.dumps(parsed.artifact.coverage.to_dict(), sort_keys=True),
            warnings=list(parsed.artifact.warnings),
        )
        inputs = [embedding_text(_context(job, c), c.text) for c in parsed.chunks]
        vectors, reused = await _vectors(job, inputs)
        job, revision = await _publish(job, parsed, vectors, lease)
    except JobFailed as e:
        return await _end(job, "cancelled" if e.code == "cancelled" else "failed", e.code, str(e))
    except _AfterRow as e:  # the row was written: the retire of older chunks runs again
        log.warning(
            "knowledge_ingest: job %s published; a step after its row failed: %s",
            job["ingestion_id"],
            error_summary(e.cause),
            exc_info=e.cause,  # its own cause chain, as the funnel raised it
        )
        return await _finish_published(await _current(job), e.revision)
    except _InsertUncertain as e:
        log.warning(
            "knowledge_ingest: job %s: the document insert failed (%s) and may yet land;"
            " its next run settles it",
            job["ingestion_id"],
            type(e.cause).__name__,
        )
        return "processing"
    except datasets.ApprovalLanded:
        return await _run_again(job)
    except _TRANSIENT as e:
        return await _retry_or_fail(job, e)
    except (JobGone, LeaseLost):
        raise  # take_and_run: nothing more is written for this run
    except Exception as e:  # one defective job must not stall the queue (logged with its traceback)
        log.exception("knowledge_ingest: job %s failed", job["ingestion_id"])
        return await _end(job, "failed", "internal", type(e).__name__)
    state = "ready" if parsed.artifact.coverage.complete else "partial"

    await _save(
        job,
        state=state,
        stage="",
        revision=revision,
        chunks=len(parsed.chunks),
        error_code="",
        error_message="",
    )
    log.info(
        "knowledge_ingest: %s/%s %s published revision %d (%d chunks, %d vectors reused)",
        job["org"],
        job["namespace"],
        job["source_uri"],
        revision,
        len(parsed.chunks),
        reused,
    )
    return state


async def take_and_run(job: dict[str, Any]) -> str | None:
    """Run `job` under its lease, renewed, after reading it again under the
    lease: None when another worker holds it, it is no longer open, an erasure
    deleted it while it ran, or the lease was lost (the job is another worker's
    then, and this run writes nothing more)."""
    from server.core import leases

    lease = await leases.take(f"knowledge-ingest:{job['ingestion_id']}", ttl_s=JOB_LEASE_S)
    if lease is None:
        return None
    try:
        async with lease.renewed():
            # Sequentially consistent: the run that held the lease before may have just
            # ended the job on another replica, and the worker takes the next job at once.
            with sequential_reads():
                current = await ingestions.get_ingestion(
                    job["org"], job["namespace"], job["ingestion_id"]
                )
            if current is None or current["state"] not in OPEN_STATES:
                return None
            # A due row read from a lagging replica can name a job that has since been put
            # back to wait out a retry delay: it is not due yet. Only a due time moved past
            # the row's counts, so a fresh job whose writer's clock runs ahead of this one
            # is not held back.
            now = datetime.now(UTC).replace(tzinfo=None)
            if current["state"] == "pending" and current["next_attempt_at"] > max(
                now, job["next_attempt_at"]
            ):
                return None
            # Its own publish first: a published revision stays published, even
            # if a cancel came after it.
            if (revision := await _own_publish(current)) is not None:
                return await _finish_published(current, revision)
            if await _overtaken(current):
                return await _end(
                    current,
                    "failed",
                    "revision_conflict",
                    "a later write of this source landed after this job began to publish",
                )
            if await _cancelled(current):
                return await _end(
                    current, "cancelled", "cancelled", "cancelled before it was published"
                )
            return await run(current, lease)
    except JobGone:
        log.info("knowledge_ingest: job %s was erased while it ran", job["ingestion_id"])
        return None
    except LeaseLost:
        log.warning("knowledge_ingest: lost the lease of job %s", job["ingestion_id"])
        return None
