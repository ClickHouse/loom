"""knowledge.restore: undo a `knowledge.delete` or `knowledge.bulk_delete`.

It restores only what carries a delete's signature (the revision's chunks listed in
`loom.chunk_supersessions` at the row's own `superseded_at`), never what an erasure
reached, and takes back exactly what it wrote when an erasure lands meanwhile or a write
fails. The answers are in docs/features/knowledge.md, "Restoring a deleted document";
the ordering and what it cannot take back in docs/architecture/failure-and-recovery.md,
"Knowledge restore".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import BackendUnavailableError, PreconditionFailedError
from server.core.pg_lease import PublicationPending
from server.core.shielded import shielded
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover
from server.knowledge.api.models_knowledge import KnowledgeRestoreInput, KnowledgeRestoreOutput
from server.knowledge.app import ingest_run
from server.knowledge.store import blobs
from server.knowledge.store import chunks as chunks_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import restore as restore_repo
from server.knowledge.store.documents import DocumentRow
from server.knowledge.store.pg_documents import DocumentErased, HeadMoved

_NO_ORIGINAL = {"blob_path": "", "blob_size": 0, "blob_content_type": "", "original_filename": ""}

# Besides the stamps, restore reads the erasure ledger, which keeps a request 90 days
# (`TTL requested_at + toIntervalDay(90)` on loom.erasure_requests): a delete older than
# the ledger reaches is past restoring. A day short of it, for clock skew.
RESTORE_WINDOW = timedelta(days=89)


async def handle_knowledge_restore(input: KnowledgeRestoreInput) -> KnowledgeRestoreOutput:
    org, ns, uri = input.org, input.namespace, input.source_uri
    # Sequential from the first read: a restore right after the caller's delete must see it.
    with own_write_reads():
        doc = await documents_repo.get_one(org, ns, uri, include_superseded=True)
        if doc is None:
            return KnowledgeRestoreOutput(restored=False, reason="not_found")
        if doc.superseded_at is None:
            return _answer(doc, restored=False, reason="live")
        if input.if_revision is not None and input.if_revision != doc.revision:
            return _answer(doc, restored=False, reason="revision_mismatch")
        if doc.superseded_at < naive_utc_now() - RESTORE_WINDOW:
            return _answer(doc, restored=False, reason="expired")
        # Run to its end under the claim; a cancelled caller's restore is taken back.
        caller_gone = asyncio.Event()
        try:
            return await shielded(
                _claimed(doc, doc.superseded_at, caller_gone), name="knowledge.restore"
            )
        except asyncio.CancelledError:
            caller_gone.set()
            raise


async def _claimed(
    doc: DocumentRow, at: datetime, caller_gone: asyncio.Event
) -> KnowledgeRestoreOutput:
    """The restore under the source's claim, from the re-read to the write back; `at` is
    the delete's time on the retired row."""
    org, ns, uri = doc.org, doc.namespace, doc.source_uri
    # Under the Postgres authority a document whose keys it cannot store is refused (422)
    # before anything is brought back: its write-back and its undo would both be refused.
    documents_repo.check_storable([doc.model_dump()])
    async with ingest_run.claim_source(org, ns, uri) as held:
        if not held:
            raise BackendUnavailableError(
                "another write of this source holds it", operation="knowledge.restore"
            )
        current = await documents_repo.get_one(org, ns, uri, include_superseded=True)
        if current is None or current.version != doc.version:
            raise PreconditionFailedError(
                "the document changed while it was restored; call knowledge.restore again"
            )
        # A delete's retirement of the chunks still to land would retire them again after
        # this restore brought them back.
        if await documents_repo.publication_may_land(org, ns, uri):
            raise BackendUnavailableError(
                "a publication of this source may still land (a parked one waits for an"
                " operator); call knowledge.restore again",
                operation="knowledge.restore",
            )
        versions = await restore_repo.retired_by_delete(org, ns, uri, doc.revision, at)
        # Without the delete's rows, a restore that failed after the chunks left them
        # live under the retired row.
        ingestions = versions or await restore_repo.live_versions(org, ns, uri, doc.revision)
        if not ingestions:
            return _answer(doc, restored=False, reason="not_restorable")
        if await _erased(doc, ingestions, signed=bool(versions)):
            if not versions:  # live leftovers of a cut-off restore: retire them again
                await _relist(doc, ingestions, at)
            return _answer(doc, restored=False, reason="not_restorable")
        return await _write_back(doc, versions, ingestions, at, caller_gone)


async def _write_back(
    doc: DocumentRow,
    versions: list[int],
    ingestions: list[int],
    at: datetime,
    caller_gone: asyncio.Event,
) -> KnowledgeRestoreOutput:
    """Make the deleted revision live, then take it back if an erasure turned up meanwhile.
    `versions` empty means the delete's rows are gone (a cut-off restore's leftovers)."""
    org, ns, uri = doc.org, doc.namespace, doc.source_uri
    signed = bool(versions)
    # Reads before any write: the undo covers everything from the first write to the answer.
    original = await _original(doc)
    dropped = _NO_ORIGINAL if original == "gone" else {}
    if doc.artifact_sha256 and await blobs.locate(org, ns, doc.artifact_sha256) is None:
        dropped = {**dropped, "artifact_sha256": ""}  # the sweep took it too
    if caller_gone.is_set():
        return _answer(doc, restored=False, reason="not_restorable")
    restored_version = documents_repo.version_past(doc.version)
    try:
        if versions:
            await restore_repo.unretire(org, ns, uri, versions, at)
        # Over the version read under the claim: a text write takes no claim, and one
        # committed since refuses this restore (under the Postgres authority).
        await documents_repo.insert_one(
            documents_repo.build_document_row(
                doc, version=restored_version, superseded_at=None, **dropped
            ),
            over=doc.version,
        )
        live = await chunks_repo.count_active_for_doc(org, ns, uri)
        # An erasure registered while this ran may have passed the rows already.
        covering = await _ledger_covering(doc, signed=signed)
    except BaseException as e:  # the claim's timeout cancels too: the undo must run
        await _undo_after_failure(doc, ingestions, restored_version, at, signed=signed)
        if isinstance(e, DocumentErased):  # an erasure removed it since: as any erasure
            return _answer(doc, restored=False, reason="not_restorable")
        if isinstance(e, PublicationPending):  # committed, then taken back: not committed
            raise BackendUnavailableError(
                "the restore was taken back; call knowledge.restore again",
                operation="knowledge.restore",
            ) from e
        raise
    if covering or caller_gone.is_set():
        await _undo(doc, ingestions, restored_version, at, purge=_purging(covering))
        return _answer(doc, restored=False, reason="not_restorable")
    return _answer(doc, restored=True, chunks_restored=live, original=original)


def _purging(covering: list[dict[str, Any]]) -> bool:
    return any(ledger_reads.purges(r) for r in covering)


async def _purge_found(doc: DocumentRow, *, signed: bool) -> bool:
    """Whether a purge may cover the document, for an undo after a failure; an unreadable
    ledger counts as none, so the row goes back retired rather than deleted."""
    try:
        return _purging(await _ledger_covering(doc, signed=signed))
    except Exception:  # noqa: BLE001 — the undo must run; the row goes back retired
        return False


async def _undo_after_failure(
    doc: DocumentRow, ingestions: list[int], restored: int, at: datetime, *, signed: bool
) -> None:
    await _undo(doc, ingestions, restored, at, purge=await _purge_found(doc, signed=signed))


async def _undo(
    doc: DocumentRow, ingestions: list[int], restored: int, at: datetime, *, purge: bool
) -> None:
    """Take back what this call wrote and retire its ingestions again at the delete's time:
    its row deleted under a purge, else the newest row written back retired."""
    org, ns, uri = doc.org, doc.namespace, doc.source_uri
    if purge:
        # Under the Postgres authority the restored head and any delivery of it go first,
        # so no publication brings the row back.
        await documents_repo.drop_committed(org, ns, uri, restored)
        await restore_repo.delete_row(org, ns, uri, restored)
    else:
        newest = await documents_repo.get_one(org, ns, uri, include_superseded=True)
        if newest is not None and newest.revision == doc.revision:
            try:
                await documents_repo.insert_one(
                    documents_repo.build_document_row(
                        newest,
                        version=documents_repo.version_past(max(restored, newest.version)),
                        superseded_at=newest.superseded_at or at,
                    ),
                    over=newest.version,
                )
            except PublicationPending:
                pass  # the retired row is committed: the delivery worker publishes it
            except (HeadMoved, DocumentErased):
                pass  # a later write took the source, or an erasure removed it
    await _relist(doc, ingestions, at)


async def _relist(doc: DocumentRow, ingestions: list[int], at: datetime) -> None:
    for version in ingestions:
        await chunks_repo.mark_doc_superseded(
            doc.org, doc.namespace, doc.source_uri, only_version=version, when=at
        )


def _answer(doc: DocumentRow, **fields: Any) -> KnowledgeRestoreOutput:
    return KnowledgeRestoreOutput(doc_id=doc.doc_id, revision=doc.revision, **fields)


async def _erased(doc: DocumentRow, ingestions: list[int], *, signed: bool) -> bool:
    """An erasure erased the original, stamped the ingestions' chunks, or one that may
    reach the document had not finished when it was written (still running, failed, or
    completed since)."""
    org, ns, uri = doc.org, doc.namespace, doc.source_uri
    if doc.blob_erased_at is not None:
        return True
    if await restore_repo.erased_chunks(org, ns, uri, ingestions):
        return True
    return bool(await _ledger_covering(doc, signed=signed))


async def _ledger_covering(doc: DocumentRow, *, signed: bool) -> list[dict[str, Any]]:
    """The ledger's erasures, unfinished when the document was written, that may reach it;
    a user's covers only its writer's revision when the delete's signature is there
    (`signed`), any user's otherwise."""
    since = int(doc.ingested_at.replace(tzinfo=UTC).timestamp())
    user = doc.user_id if signed else None
    rows = await ledger_reads.unfinished_reaching(
        doc.org, doc.namespace, user, since, ledger_reads.LEDGER_CLOCK_MARGIN_S
    )
    return [
        r for r in rows if may_cover(str(r["scope_type"]), str(r["scope_value"]), doc.namespace)
    ]


async def _original(doc: DocumentRow) -> Literal["kept", "gone", "none"]:
    if not doc.blob_path:
        return "none"
    held = await blobs.locate(doc.org, doc.namespace, doc.blob_path)
    return "kept" if held is not None else "gone"
