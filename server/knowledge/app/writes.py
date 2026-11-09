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
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core import pg
from server.core.error_summary import error_summary
from server.core.errors import BackendUnavailableError, PreconditionFailedError, ValidationError
from server.core.hashing import sha256_document_text as _content_sha256
from server.core.pg_lease import PublicationPending
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
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store.pg_documents import DocumentErased, HeadMoved
from server.settings import settings

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


def _published_anyway(e: BaseException) -> bool:
    """Whether a document write that raised `e` may still serve its row: committed under
    the Postgres authority (the worker publishes it), its row in with only the
    retirement failed, or a commit whose answer was lost. Its chunks then stay."""
    from server.knowledge.store.documents import OlderNotRetired

    return isinstance(e, PublicationPending | OlderNotRetired) or pg.may_have_committed(e)


async def _drop_own_chunks(
    org: str,
    namespace: str,
    own: list[tuple[str, int]],
    refusal: BaseException,
    now: datetime,
    embedding_model: str,
) -> None:
    """The chunks of a write whose document did not land (`own`: each (source, version)
    this write inserted, embedded with `embedding_model`) serve no published version and
    go before the error answers: retired, by rows that also cover chunks of an insert
    still to land, and deleted when an erasure removed the document the write was built
    from, so its text and author do not outlive the erasure."""
    from server.knowledge.store import chunks as knowledge_repo

    if isinstance(refusal, DocumentErased):  # first, and whatever the retirement does
        try:
            await knowledge_repo.delete_versions(org, namespace, own)
        except Exception as e:  # noqa: BLE001 — the write's own error is the answer, not this
            log.warning("knowledge write: its erased chunks stay: %s", error_summary(e))
    try:
        await knowledge_repo.retire_versions(
            org, namespace, own, embedding_model=embedding_model, when=now
        )
    except Exception as e:  # noqa: BLE001 — the write's own error is the answer, not this one
        log.warning(
            "knowledge write: its unpublished chunks of %d sources stay: %s",
            len(own),
            error_summary(e),
        )


# Clock skew between hosts allowed for when an orphan's age is judged by its writer's
# `time_ns` version stamp.
_CLOCK_SKEW_MARGIN_S = 300  # not a setting: the orphan age setting widens the window


async def _retire_orphans(
    org: str, namespace: str, source_uris: list[str], now: datetime
) -> dict[str, int]:
    """Retire the chunks of sources with no document row whose version (a `time_ns`
    stamp) is older than the orphan age, and answer how many each source had: every
    writer inserts a new source's chunks before its row, so a younger version may be a
    first write's still to commit, which keeps its chunks; one that fails retires its own.
    The age is LOOM_KNOWLEDGE_ORPHAN_CHUNK_MIN_AGE_SECONDS, never less than an ingest
    pass and a margin for clock skew, the longest a writer holds chunks before its row."""
    from server.knowledge.store import chunks as knowledge_repo

    if not source_uris:
        return {}
    age_s = max(
        settings.knowledge_orphan_chunk_min_age_seconds,
        settings.knowledge_ingest_max_pass_seconds + _CLOCK_SKEW_MARGIN_S,
    )
    cut = time.time_ns() - int(age_s * 1_000_000_000)
    old = await knowledge_repo.count_active_by_uri(org, namespace, source_uris, before_version=cut)
    if old:
        await knowledge_repo.mark_docs_superseded(org, namespace, dict.fromkeys(old, cut), when=now)
    return old


async def _retire_leftovers(
    org: str, namespace: str, source_uri: str, *, operation: str
) -> int | None:
    """Retire a deleted document's leftover chunks, under the source's claim, which
    restore holds from its read to its write-back, read again under it (the caller's
    reads are sequentially consistent): one restored or written again meanwhile is live
    then, keeps them, and this answers None. They go up to its version (a write that
    inserted newer chunks and has not committed keeps them), stamped at its delete as its
    tombstone retires them: under the Postgres authority that retirement may still be
    owed, and restore finds the delete by that stamp. Answers how many it retired. A
    claim another write holds answers a retryable 503. A source with no document row is
    `_retire_orphans`'s."""
    from server.knowledge.app import ingest_run
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    async with ingest_run.claim_source(org, namespace, source_uri) as held:
        if not held:
            raise BackendUnavailableError(
                "another write of this source holds it", operation=operation
            )
        doc = await documents_repo.get_one(org, namespace, source_uri, include_superseded=True)
        if doc is None or doc.superseded_at is None:
            return None  # restored or written since
        counts = await knowledge_repo.count_active_by_uri(
            org, namespace, [source_uri], before_version=doc.version + 1
        )
        await knowledge_repo.mark_doc_superseded(
            org,
            namespace,
            source_uri,
            before_version=doc.version + 1,
            when=doc.superseded_at,
        )
    return counts.get(source_uri, 0)


def _live_id(prior: DocumentRow | None) -> str:
    """The `doc_id` of the live document a write carries columns from (its original, its
    author), which an erasure since refuses (`documents.insert_one`'s `built_from`)."""
    return prior.doc_id if prior is not None and prior.superseded_at is None else ""


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


def next_revision(prior: DocumentRow | None) -> int:
    """The revision a new version of the source publishes: one past the
    latest, deleted or not. Sequential writes never reuse one. Ingests of a
    source publish under its claim, one at a time; a text write takes no claim.
    Under the Postgres authority the second of two racing writes of a source is
    refused (`HeadMoved`); under ClickHouse's both can number the same revision
    (the later version row wins)."""
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
    size = settings.knowledge_chunk_size
    # The two are set one at a time: a step past the window would skip text unindexed.
    stride = min(settings.knowledge_chunk_stride, size)
    return chunk_document(content, size=size, stride=stride)


async def handle_knowledge_upsert(
    input: KnowledgeUpsertInput, *, read_version: int | None = None
) -> KnowledgeUpsertOutput:
    """Insert new chunks for `source_uri`; soft-supersede prior chunks.

    Used when an upstream source changes after initial ingestion. Searches
    return the new content by default; superseded chunks remain queryable
    with explicit `include_superseded=true`.

    Dedup is warn-don't-block: a canonical sha256 over content+title and
    over each chunk surfaces `duplicate_of_uri` (a live doc under a
    different `source_uri` with the same doc hash) and
    `duplicate_chunk_count` (newly-inserted chunks whose hash already
    lives under a different source_uri).

    `read_version`: the version an in-process caller built `input` from (a reindex, a
    rechunk); the write commits over it, so under the Postgres authority a source
    written since refuses it (HeadMoved, 409) and the caller's copy never replaces a
    newer write.
    """
    from server.knowledge.store import chunks as knowledge_repo

    author = authorship(input)

    embedder = get_embedder()
    await codes_state.refuse_width("chunks", embedder.dim)
    doc_id = str(uuid.uuid4())
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
    from server.knowledge.store import documents as documents_repo

    # Read even when nothing is live: a deleted document's revision still counts.
    prior = await documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    # The document row and every chunk row record the same revision.
    revision = next_revision(prior)
    uploader = author["user_id"]  # the writer's, even when the document keeps its author
    writer = author["actor"]
    author = _author_for(prior, input.content, author, _fresh_sha(input))
    _require_source_tags_kept(prior, writer, author["actor"], input.tags)
    # Refused before anything is written, the original's bytes included.
    documents_repo.check_storable(
        [{**author, "org": input.org, "namespace": input.namespace, "source_uri": input.source_uri}]
    )
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(
        input, _original_of(prior, author), uploader=uploader
    )

    # The new version's chunks first, then its document row, and only then the older
    # versions' chunks retired (`retire_older`): a store that fails or is busy leaves the
    # current version searchable, not its chunks retired with no new version.
    chunks = _chunk_text(input.content)
    contexts = _contexts(chunks, input.title, input.description)
    embeddings = await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts))
    # Stamped once embedded: a version is the clock when its chunks are written, which a
    # delete's orphan age measures (`_retire_orphans`).
    version = documents_repo.version_past(documents_repo.version_of(prior))
    rows = knowledge_repo.build_chunk_rows(
        org=input.org,
        namespace=input.namespace,
        source_uri=input.source_uri,
        title=input.title,
        chunks=chunks,
        contexts=contexts,
        embeddings=embeddings,
        tags=input.tags,
        metadata=json.dumps(input.metadata, sort_keys=True),
        doc_version=version,
        revision=revision,
        doc_format="",
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        now=now,
        author=author,
    )
    doc_row = documents_repo.build_document_row(
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
        revision=revision,
    )
    try:
        # Inside the cleanup: a chunk insert that timed out may land later.
        await knowledge_repo.insert_chunks(rows)
        # Written over the version read (the caller's, when it built the input): under
        # the Postgres authority a write of the source that committed since refuses this
        # one (`pg_documents.HeadMoved`).
        await documents_repo.insert_one(
            doc_row,
            over=documents_repo.version_of(prior) if read_version is None else read_version,
            # Always: a count taken before the prior read may miss chunks a write
            # committed since; retiring none costs one statement.
            retire_older=True,
            built_from=_live_id(prior),
            chunked=True,
        )
    except BaseException as e:  # re-raised: the chunks of a write that did not land go first
        if not _published_anyway(e):
            # Shielded: a cancelled request's cleanup still runs.
            await asyncio.shield(
                _drop_own_chunks(
                    input.org,
                    input.namespace,
                    [(input.source_uri, version)],
                    e,
                    now,
                    embedder.model_name,
                )
            )
        raise

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
    now = datetime.now(UTC).replace(tzinfo=None)

    with traced("chunk.text", content_len=len(input.content)):
        chunks = _chunk_text(input.content)

    # Carry a prior blob forward across a metadata-only edit (no fresh
    # bytes) so the user's download capability isn't silently dropped.
    from server.knowledge.store import documents as documents_repo

    prior = await documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    # The document row and every chunk row record the same revision.
    revision = next_revision(prior)
    uploader = author["user_id"]  # the writer's, even when the document keeps its author
    writer = author["actor"]
    author = _author_for(prior, input.content, author, _fresh_sha(input))
    _require_source_tags_kept(prior, writer, author["actor"], input.tags)
    # Refused before anything is written, the original's bytes included.
    documents_repo.check_storable(
        [{**author, "org": input.org, "namespace": input.namespace, "source_uri": input.source_uri}]
    )
    blob_path, blob_size, blob_content_type, original_filename = await _resolve_blob(
        input, _original_of(prior, author), uploader=uploader
    )

    from server.knowledge.store import chunks as knowledge_repo

    with traced("embed.chunks", model=embedder.model_name, chunk_count=len(chunks)):
        contexts = _contexts(chunks, input.title, input.description)
        embeddings = await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts))
        # Stamped once embedded, as upsert's.
        version = documents_repo.version_past(documents_repo.version_of(prior))
        rows = knowledge_repo.build_chunk_rows(
            org=input.org,
            namespace=input.namespace,
            source_uri=input.source_uri,
            title=input.title,
            chunks=chunks,
            contexts=contexts,
            embeddings=embeddings,
            tags=input.tags,
            metadata=json.dumps(input.metadata, sort_keys=True),
            doc_version=version,
            revision=revision,
            doc_format="",
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            now=now,
            author=author,
        )

    # The builder sets every loom.documents column, content_sha256 included
    # so cross-URI dedup can match.
    doc_row = documents_repo.build_document_row(
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
        revision=revision,
    )

    # The chunks first, as upsert and bulk add: a commit that answers PUBLICATION_PENDING
    # is published by the delivery worker, which publishes the document row alone.
    try:
        # Inside the cleanup: a chunk insert that timed out may land later.
        with traced("ch.insert.chunks", table="loom.chunks", chunk_count=len(rows)):
            await knowledge_repo.insert_chunks(rows)
        with traced("ch.insert.documents", table="loom.documents"):
            await documents_repo.insert_one(
                doc_row,
                over=documents_repo.version_of(prior),
                built_from=_live_id(prior),
                # Under the Postgres authority search serves the head's chunks alone, so an
                # add retires the older ones (a failed write's left behind included), as an
                # upsert does, rather than keep chunks nothing serves.
                retire_older=pg.enabled(),
                chunked=True,
            )
    except BaseException as e:  # re-raised: the chunks of a write that did not land go first
        if not _published_anyway(e):
            # Shielded: a cancelled request's cleanup still runs.
            await asyncio.shield(
                _drop_own_chunks(
                    input.org,
                    input.namespace,
                    [(input.source_uri, version)],
                    e,
                    now,
                    embedder.model_name,
                )
            )
        raise

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
    """Tombstone a live document, chunks or none; its publication retires the older chunk
    versions. A source with no document row has its chunks older than the orphan age
    retired (`_retire_orphans`), and answers how many. deleted=False for a source with
    nothing, one with no document row whose chunks are all younger than the orphan age,
    one already deleted with no active chunks, or none up to its tombstone (a newer write's
    still to commit; only its leftover datasets go), or one restored or written again
    before the leftovers' claim (`_retire_leftovers`); an already-deleted source with
    leftover chunks has them retired up to its tombstone and answers deleted=True when any
    were.
    Every read is sequentially consistent, as a restore's are: a delete right after a
    write sees it on any replica.
    """
    from server.core.ch import own_write_reads

    with own_write_reads():
        return await _delete(input)


async def _delete(input: KnowledgeDeleteInput) -> KnowledgeDeleteOutput:
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    now = datetime.now(UTC).replace(tzinfo=None)

    active = await knowledge_repo.count_active_for_doc(
        input.org,
        input.namespace,
        input.source_uri,
    )

    # The doc row regardless of chunk count: a doc whose chunks were never embedded still
    # needs its tombstone, or it lingers in the Library list.
    doc = await documents_repo.get_one(
        input.org,
        input.namespace,
        input.source_uri,
        include_superseded=True,
    )
    if doc is None:
        if active == 0:
            return KnowledgeDeleteOutput(deleted=False, chunks_tombstoned=0)
        # No document, chunks left (a failed write's cleanup that failed too, a crash, a
        # TTL that took the row): those old enough to belong to no write still to commit go.
        old = await _retire_orphans(input.org, input.namespace, [input.source_uri], now)
        n = old.get(input.source_uri, 0)
        return KnowledgeDeleteOutput(deleted=n > 0, chunks_tombstoned=n)

    # Re-delete of an already-deleted doc: deleted=False lets callers
    # distinguish a real first delete from a re-ask on something gone.
    if active == 0 and doc.superseded_at is not None:
        # A snapshot an earlier delete could not reach (one an approval wrote as it
        # ran, or one N-1 left) goes now; one catalog read when there is none.
        await datasets_repo.forget_published(
            input.org, input.namespace, {input.source_uri: doc.version}
        )
        return KnowledgeDeleteOutput(deleted=False, chunks_tombstoned=0)

    if doc.superseded_at is None:
        # The tombstone retires the chunks once it is the source's head, so a delete
        # that loses the head to another write leaves that write's chunks alone.
        try:
            await documents_repo.insert_one(
                documents_repo.build_document_row(
                    doc, version=documents_repo.version_past(doc.version), superseded_at=now
                ),
                over=doc.version,
                retire_older=True,
            )
        except PublicationPending:  # committed: its datasets go now, as on success
            await datasets_repo.forget_published(
                input.org, input.namespace, {input.source_uri: doc.version}
            )
            raise
    elif active > 0:
        # Counted by what went: a newer write's chunks, still to commit, stay.
        left = await _retire_leftovers(
            input.org, input.namespace, input.source_uri, operation="knowledge.delete"
        )
        if left is None:  # restored meanwhile: nothing of it is deleted
            return KnowledgeDeleteOutput(deleted=False, chunks_tombstoned=0)
        active = left
    # Unserved once the document is gone; deleted without waiting.
    await datasets_repo.forget_published(
        input.org, input.namespace, {input.source_uri: doc.version}
    )

    # A re-ask on a deleted source whose only chunks are a newer write's still to commit
    # deleted nothing, as `active == 0` above.
    return KnowledgeDeleteOutput(
        deleted=doc.superseded_at is None or active > 0, chunks_tombstoned=active
    )


# ============================================================
# knowledge.update_metadata — title / description / tags edit
# ============================================================


async def _edit_ingested(
    input: KnowledgeUpdateMetadataInput, read: DocumentRow, edited: dict[str, Any]
) -> None:
    """A metadata edit of an ingested document, under the source's claim (which
    an ingest's publish and a dataset approval take): its datasets are carried
    to the new version first, so the edit keeps serving them and no approval
    lands between the carry and the row. Refused when the source moved since
    it was read."""
    from server.core.ch import own_write_reads
    from server.knowledge.app import datasets
    from server.knowledge.app.ingest_run import claim_source
    from server.knowledge.store import documents as documents_repo

    org, ns, uri = input.org, input.namespace, input.source_uri
    with own_write_reads():
        async with claim_source(org, ns, uri) as held:
            if not held:
                raise BackendUnavailableError(
                    "another write of this source holds it", operation="knowledge.update_metadata"
                )
            live = await documents_repo.get_one(org, ns, uri)
            if live is None or live.version != read.version:
                raise PreconditionFailedError(
                    "the document changed while it was edited; read it again"
                )
            version = documents_repo.version_past(live.version)
            row = documents_repo.build_document_row(live, version=version, **edited)
            documents_repo.check_storable([row])  # refused before the datasets are carried
            await datasets.carry(org, ns, uri, live.version, version)
            try:
                await documents_repo.insert_one(row, over=live.version)
            except BaseException as e:  # re-raised: an edit that did not land forgets them
                if not _published_anyway(e):
                    # Shielded: a cancelled request's cleanup still runs.
                    await asyncio.shield(datasets_repo.forget(org, ns, [uri], only=version))
                raise


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

    edited = {"title": next_title, "description": next_description, "tags": next_tags}
    # No-op if nothing changed, so idempotent re-applies don't pile up versions; a
    # reindex still runs, so a retry after one that did not finish completes it.
    changed = (
        next_title != existing.title
        or next_description != existing.description
        or list(next_tags) != list(existing.tags)
    )
    pending: PublicationPending | None = None
    read_version = existing.version  # the version the reindex rewrites over
    if changed and existing.ingestion_id:
        await _edit_ingested(input, existing, edited)
    elif changed:
        read_version = documents_repo.version_past(existing.version)
        try:
            await documents_repo.insert_one(
                documents_repo.build_document_row(existing, version=read_version, **edited),
                over=existing.version,
            )
        except PublicationPending as e:  # committed: the reindex below still runs
            pending = e

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
        try:
            upsert_out = await handle_knowledge_upsert(upsert_in, read_version=read_version)
        except HeadMoved:
            if not changed:  # this call wrote nothing
                raise
            # The edit committed (published or still owed): only the reindex was refused.
            raise PreconditionFailedError(
                "the metadata was committed; its reindex was refused because another write"
                " took the source since: call knowledge.update_metadata with reindex=true"
                " again"
            ) from None
        chunks_reindexed = upsert_out.chunks_superseded
    if pending is not None:
        raise pending

    return KnowledgeUpdateMetadataOutput(
        updated=changed,
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

    # One row per source per commit (`pg_documents.commit` refuses a repeat): refused
    # here, before anything is written.
    uris = [e.source_uri for e in input.entries]
    repeated = sorted(u for u, n in Counter(uris).items() if n > 1)
    if repeated:
        named = ", ".join(repeated[:5])
        more = f" and {len(repeated) - 5} more" if len(repeated) > 5 else ""
        raise ValidationError(f"entries name a source_uri more than once: {named}{more}")

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
            uris,
            include_superseded=True,
        )
    }

    # Every entry is checked before any is chunked or embedded: a refusal writes nothing.
    authors = []
    for entry in input.entries:
        prior_doc = priors.get(entry.source_uri)
        authors.append(_author_for(prior_doc, entry.content, author))
        _require_source_tags_kept(prior_doc, author["actor"], authors[-1]["actor"], entry.tags)
        documents_repo.check_storable(
            [
                {
                    **authors[-1],
                    "org": input.org,
                    "namespace": input.namespace,
                    "source_uri": entry.source_uri,
                }
            ]
        )

    # Every entry embedded before any version is stamped: a version is the clock when its
    # chunks are written, which a delete's orphan age measures (`_retire_orphans`).
    embedded = []
    for entry in input.entries:
        chunks = _chunk_text(entry.content)
        contexts = _contexts(chunks, entry.title, entry.description)
        vectors = await _embed_inline_batch(embedder, _embedding_texts(chunks, contexts))
        embedded.append((chunks, contexts, vectors))
    for entry, entry_author, (chunks, contexts, vectors) in zip(
        input.entries, authors, embedded, strict=True
    ):
        doc_id = str(uuid.uuid4())
        version = documents_repo.version_past(
            documents_repo.version_of(priors.get(entry.source_uri))
        )
        revision = next_revision(priors.get(entry.source_uri))
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
                revision=revision,
            )
        )

        chunk_rows.extend(
            knowledge_repo.build_chunk_rows(
                org=input.org,
                namespace=input.namespace,
                source_uri=entry.source_uri,
                title=entry.title,
                chunks=chunks,
                contexts=contexts,
                embeddings=vectors,
                tags=entry.tags,
                metadata=json.dumps(entry.metadata, sort_keys=True),
                doc_version=version,
                revision=revision,
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

    # The chunks first: a commit that answers PUBLICATION_PENDING is published by the
    # delivery worker, which publishes the document rows alone.
    try:
        # Inside the cleanup: a chunk insert that timed out may land later.
        await knowledge_repo.insert_chunks(chunk_rows)
        await documents_repo.insert_many(
            doc_rows,
            over={uri: d.version for uri, d in priors.items()},
            built_from=[i for i in map(_live_id, priors.values()) if i],
            retire_older=set(uris) if pg.enabled() else frozenset(),  # as an add's
            chunked=True,
        )
    except BaseException as e:  # re-raised: the chunks of a write that did not land go first
        if not _published_anyway(e):
            own = [(str(r["source_uri"]), int(r["version"])) for r in doc_rows]
            await asyncio.shield(
                _drop_own_chunks(input.org, input.namespace, own, e, now, embedder.model_name)
            )
        raise

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
    """Delete the listed sources: one tombstone row per live document in a single
    commit, whose publication retires each source's chunks up to its tombstone, as
    `knowledge.delete` does, chunks or none. First it retires the leftovers of an
    already-deleted source up to that tombstone, under its claim (`_retire_leftovers`; one
    restored meanwhile counts 0), and a source with no document row's old enough chunks
    (`_retire_orphans`), as `knowledge.delete` does. Answers per-uri chunk counts, and deletes
    the datasets of the versions the tombstones retire. Every read is sequentially
    consistent, as `knowledge.delete`'s."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        return await _bulk_delete(input)


async def _bulk_delete(input: KnowledgeBulkDeleteInput) -> KnowledgeBulkDeleteOutput:
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    now = datetime.now(UTC).replace(tzinfo=None)
    uris = list(dict.fromkeys(input.source_uris))  # a source named twice is deleted once
    # Each source's latest row before this delete, superseded or not: the datasets
    # of the versions up to it go, so a source deleted before (by this build or an
    # older one, or as an approval landed) loses what is left. An ingest running
    # meanwhile writes a later version, which this leaves to it.
    latest = await documents_repo.get_many_by_uris(
        input.org, input.namespace, uris, include_superseded=True
    )
    retired = {d.source_uri: d.version for d in latest}

    counts = await knowledge_repo.count_active_by_uri(
        input.org,
        input.namespace,
        uris,
    )
    existing_docs = [d for d in latest if d.superseded_at is None]
    live_uris = {d.source_uri for d in existing_docs}
    orphans = [u for u in uris if counts.get(u, 0) and u not in retired]
    old = await _retire_orphans(input.org, input.namespace, orphans, now)  # as the delete's
    for u in orphans:  # counted by what went: a first write's young chunks stay
        counts[u] = old.get(u, 0)
    for u in uris:  # each deleted source's leftovers, as `knowledge.delete`'s
        if u in live_uris or u not in retired or not counts.get(u, 0):
            continue
        left = await _retire_leftovers(
            input.org, input.namespace, u, operation="knowledge.bulk_delete"
        )
        if left is None:  # restored meanwhile: nothing of it is deleted
            counts.pop(u)
            retired.pop(u)
        else:  # counted by what went, as a delete's
            counts[u] = left
    total = sum(counts.values())
    if existing_docs:
        # Supersede the live document rows: re-INSERT with superseded_at set and bumped
        # versions; each tombstone retires its source's chunks (`documents.publish`).
        doc_rows = [
            documents_repo.build_document_row(
                d, version=documents_repo.version_past(d.version), superseded_at=now
            )
            for d in existing_docs
        ]
        try:
            await documents_repo.insert_many(
                doc_rows,
                over={d.source_uri: d.version for d in existing_docs},
                retire_older=live_uris,
            )
        except PublicationPending:  # committed: their datasets go now, as on success
            await datasets_repo.forget_published(input.org, input.namespace, retired)
            raise
    await datasets_repo.forget_published(input.org, input.namespace, retired)

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
