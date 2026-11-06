"""knowledge.ingest / ingest_status / ingest_cancel: a job that has the extractor
service extract an upload and publishes it as a document revision (`ingest_run`);
knowledge.formats: the formats those services read.

An ingest names an upload of the same namespace that the caller made and that
has not expired; the job carries the caller's authorship, so the worker writes
the document as theirs. A status read is the namespace's viewers'; a cancel
the namespace's writers'.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from server.core.errors import NotFoundError, PreconditionFailedError
from server.knowledge.api.models_knowledge import (
    KnowledgeFormat,
    KnowledgeFormatsInput,
    KnowledgeFormatsOutput,
    KnowledgeIngestCancelInput,
    KnowledgeIngestCancelOutput,
    KnowledgeIngestInput,
    KnowledgeIngestOutput,
    KnowledgeIngestStatusInput,
    KnowledgeIngestStatusOutput,
)
from server.knowledge.store import ingestions


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def handle_knowledge_ingest(input: KnowledgeIngestInput) -> KnowledgeIngestOutput:
    from server.knowledge.app.writes import authorship

    author = authorship(input)
    upload_id = _uuid(input.upload_id)
    upload = (
        await ingestions.get_upload(input.org, input.namespace, upload_id) if upload_id else None
    )
    # Another writer's upload is not found either: an upload is its uploader's to ingest.
    if upload is None or upload["actor"] != author["actor"]:
        raise NotFoundError("no upload of this id in the namespace")
    if upload["expires_at"] <= datetime.now(UTC).replace(tzinfo=None):
        raise PreconditionFailedError("the upload expired; upload the file again")
    row = ingestions.ingestion_row(
        **author,
        org=input.org,
        namespace=input.namespace,
        ingestion_id=str(uuid.uuid4()),
        source_uri=input.source_uri,
        upload_id=upload["upload_id"],
        sha256=upload["sha256"],
        filename=upload["filename"],
        content_type=upload["content_type"],
        title=input.title or upload["filename"],
        description=input.description,
        tags=input.tags,
        metadata=json.dumps(input.metadata, sort_keys=True),
        options=input.options.model_dump_json(),
        expected_revision=-1 if input.expected_revision is None else input.expected_revision,
        allow_partial=input.allow_partial,
        state="pending",
    )
    await ingestions.insert_ingestion(row)
    return KnowledgeIngestOutput(
        ingestion_id=row["ingestion_id"],
        state="pending",
        source_uri=input.source_uri,
        namespace=input.namespace,
    )


def _retrieval(state: str) -> str:
    return {"ready": "ready", "partial": "ready", "failed": "failed", "cancelled": "cancelled"}.get(
        state, "pending"
    )


def _status(job: dict[str, Any], cancelled: bool) -> KnowledgeIngestStatusOutput:
    """The job as the status op answers it. A pending job with a cancel is
    `cancelled` already: no worker will run it (`take_and_run` ends it)."""
    if cancelled and job["state"] == "pending":
        job = {**job, "state": "cancelled", "error_code": "cancelled"}
        job["error_message"] = "cancelled before it ran"
    expected = int(job["expected_revision"])
    return KnowledgeIngestStatusOutput(
        found=True,
        ingestion_id=job["ingestion_id"],
        source_uri=job["source_uri"],
        state=job["state"],
        stage=job["stage"],
        attempts=int(job["attempts"]),
        error_code=job["error_code"],
        error_message=job["error_message"],
        format=job["format"],
        coverage=json.loads(job["coverage"]) if job["coverage"] else None,
        warnings=list(job["warnings"]),
        chunks=int(job["chunks"]),
        revision=int(job["revision"]),
        expected_revision=None if expected < 0 else expected,
        filename=job["filename"],
        retrieval=_retrieval(job["state"]),
        cancel_requested=cancelled,
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


async def handle_knowledge_ingest_status(
    input: KnowledgeIngestStatusInput,
) -> KnowledgeIngestStatusOutput:
    from server.core.ch import own_write_reads

    job_id = _uuid(input.ingestion_id)
    with own_write_reads():
        job = await ingestions.get_ingestion(input.org, input.namespace, job_id) if job_id else None
        if job is None:
            return KnowledgeIngestStatusOutput(found=False, ingestion_id=input.ingestion_id)
        cancelled = await ingestions.cancel_requested(
            input.org, input.namespace, job["ingestion_id"]
        )
    return _status(job, cancelled)


async def handle_knowledge_ingest_cancel(
    input: KnowledgeIngestCancelInput,
) -> KnowledgeIngestCancelOutput:
    """A pending job is cancelled at once; a running one stops before it
    publishes; a finished one stays as it ended. The cancel is a row of its own
    (`ingestions.insert_cancel`), and the job is read again after it is written:
    still pending then, no worker has started it, and the one that takes it
    reads the cancel first."""
    from server.core.ch import own_write_reads
    from server.knowledge.app.writes import authorship_of

    job_id = _uuid(input.ingestion_id)
    with own_write_reads():
        job = await ingestions.get_ingestion(input.org, input.namespace, job_id) if job_id else None
        if job is None:
            return KnowledgeIngestCancelOutput(found=False)
        if job["state"] not in ingestions.OPEN_STATES:
            return KnowledgeIngestCancelOutput(found=True, state=job["state"], cancelled=False)
        job_id = job["ingestion_id"]
        author = authorship_of(agent="", session_id="", source_class=None)
        await ingestions.insert_cancel(input.org, input.namespace, job_id, author)
        job = await ingestions.get_ingestion(input.org, input.namespace, job_id)
    if job is None:  # erased meanwhile
        return KnowledgeIngestCancelOutput(found=False)
    if job["state"] == "pending":
        return KnowledgeIngestCancelOutput(found=True, state="cancelled", cancelled=True)
    if job["state"] == "processing":
        return KnowledgeIngestCancelOutput(found=True, state="processing", cancelled=True)
    # It ended between the two reads: a published revision stays published.
    return KnowledgeIngestCancelOutput(
        found=True, state=job["state"], cancelled=job["state"] == "cancelled"
    )


async def handle_knowledge_formats(input: KnowledgeFormatsInput) -> KnowledgeFormatsOutput:
    """Each known format with the names that select it and whether a configured
    extractor declares it (each service read at most once a minute)."""
    from server.core.document_formats import (
        DEFAULT_FORMAT,
        KNOWN_FORMATS,
        legacy_extensions,
        names_of,
    )
    from server.knowledge.app.extractors import available_formats
    from server.settings import settings

    served = await available_formats()
    formats = []
    for fmt in sorted(KNOWN_FORMATS):
        exts, media = names_of(fmt)
        formats.append(
            KnowledgeFormat(format=fmt, extensions=exts, media_types=media, available=fmt in served)
        )
    return KnowledgeFormatsOutput(
        formats=formats,
        max_upload_bytes=settings.knowledge_upload_max_bytes,
        refused_extensions=legacy_extensions(),
        default_format=DEFAULT_FORMAT,
    )
