"""Document-original sweep: `loom.document_blobs` rows no live document names,
and bucket objects no row names.

The one place outside erasure that deletes an original. A document delete, a
re-upload with other bytes and a retention TTL on `loom.documents` all leave
the old original behind; this pass deletes every original that no live
document (`LIVE_ORIGINAL_SQL`) names and nothing has written for at least
`document_blob_min_age_seconds`. A write stores its original before the
document row that names it, so a younger original may belong to a write still
in flight; the delete repeats the age check, so an original stored again
between the read and the delete keeps its new row.

It walks one namespace at a time, each read bounded by the namespace and a
batch, so no statement scans every tenant's documents at once.

With a bucket configured (`document_s3_bucket`) the objects of the rows it
deletes go with them; a row naming an object it cannot reach (another bucket,
or no bucket configured) is kept and recorded as the pass's failure. Before
the namespaces, the pass walks the bucket's originals a page at a time from
where the last pass stopped (the pass cursor), since that walk is bounded and
the namespace walk is not, and deletes each object no catalog row names that
is older than the same age (the catalog read with sequential consistency): the
object of a row a re-upload replaced, of a write whose row never landed, or
one whose delete failed. Each page is one listing, a catalog read by the
table's sort key (as many keys to a statement as one parameter carries), one
delete, and, for each original no row holds, one listing of its prefix (four
at a time) and a delete of its upload records, all bounded by the page.

Erasure does not wait for this: its own step deletes every original of the
scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.core import object_store as objects
from server.core.background import gather_or_cancel
from server.knowledge.store import blobs
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Originals read per statement, as many as one bound parameter carries (the
# delete binds them all, `ch_params.FIELD_BYTES`); at most _MAX_BATCHES of them per
# namespace per pass, and the next pass continues.
_BATCH = 800  # not a setting: what one parameter carries
_MAX_BATCHES = 25
# Namespaces listed per read of the walk.
_NAMESPACES = 500

# Bucket keys listed per page; `document_blob_gc_object_pages` of them a pass, and
# the next pass continues from the cursor.
_OBJECT_PAGE = 1000  # not a setting: the S3 listing's limit


@dataclass(frozen=True)
class BlobGcResult:
    deleted: int  # originals deleted this pass
    namespaces: int  # (org, namespace) pairs they came from
    objects: int = 0  # bucket objects no row named, deleted this pass


async def sweep_namespace(org: str, namespace: str, min_age_s: int) -> int:
    """Delete the namespace's unreferenced originals; returns how many went. The
    sha cursor moves past every batch, so a row the delete spared is not read
    again this pass."""
    deleted, after = 0, ""
    for _ in range(_MAX_BATCHES):
        found = await blobs.unreferenced(org, namespace, min_age_s, _BATCH, after=after)
        if not found:
            break
        result = await blobs.delete(org, namespace, found, min_age_s=min_age_s)
        deleted += result.gone
        if result.kept:
            pass_context.note_failure(
                RuntimeError(
                    f"{result.kept} object(s) are outside LOOM_DOCUMENT_S3_BUCKET, or it is"
                    " unset; the rows naming them are kept"
                ),
                "document_blob_gc: unreachable objects",
            )
        if result.error is not None:
            # The rows are gone and their upload records kept; the walk retries the objects.
            pass_context.note_failure(result.error, "document_blob_gc: object delete")
        if len(found) < _BATCH:
            break
        after = found[-1]
    return deleted


async def _sweep_bucket(min_age_s: int) -> int:
    """Up to `document_blob_gc_object_pages` pages of the bucket from the pass
    cursor; the cursor left is where the next pass starts. At the end of the
    bucket it is the root prefix itself, which sorts before every key under it,
    so the next walk starts over: the supervisor keeps only a cursor that is not
    empty.

    A page whose delete the store refuses is recorded as the pass's failure and
    passed over, so one object the store keeps does not stop the walk; the next
    walk tries it again. Keys Loom writes fit the cursor (`object_store.MAX_KEY`);
    a longer key some other writer left is kept cut short, which sorts before it,
    so the next pass reads again only the keys sharing that prefix."""
    swept, after = 0, pass_context.resume_cursor()
    bucket = settings.document_s3_bucket
    for _ in range(int(settings.document_blob_gc_object_pages)):
        try:
            page = await blobs.unnamed_objects(after, _OBJECT_PAGE, min_age_s)
        except Exception as e:  # noqa: BLE001 — the namespace walk must still run this pass
            # The bucket cannot be listed or the catalog read: the walk stops here,
            # and the next pass resumes from this cursor.
            pass_context.note_failure(e, "document_blob_gc: bucket walk")
            break
        after = page.after
        try:
            await objects.delete(bucket, page.keys)
            swept += len(page.keys)
        except objects.ObjectStoreError as e:
            # The upload records stay; the next walk tries the objects again.
            pass_context.note_failure(e, "document_blob_gc: bucket delete")
        else:
            try:
                await _forget(page, bucket, min_age_s)
            except Exception as e:  # noqa: BLE001 — the namespace walk must still run this pass
                pass_context.note_failure(e, "document_blob_gc: forget uploads")
        # Set after every page, so a pass cut at its deadline keeps this page's progress.
        pass_context.set_cursor(after or objects.root_prefix())
        if not after:
            break
    return swept


async def _forget(page: blobs.Unnamed, bucket: str, min_age_s: int) -> None:
    """The upload records of the originals no row holds whose prefix lists empty
    once the page's delete went: another write of one (younger, or on another
    page) keeps its records. The listings share the store's listing threads. A
    listing the store fails keeps its original's records, and with its objects
    already gone no later walk offers it again: the records (who uploaded which
    bytes, no content) stay until an erasure of that user or namespace."""

    async def empty(key: tuple[str, str, str]) -> bool:
        org, ns, sha = key
        prefix = objects.original_prefix(org, ns, sha)
        try:
            return not await objects.count_under(bucket, prefix)
        except objects.ObjectStoreError as e:
            pass_context.note_failure(e, "document_blob_gc: bucket recount")
            return False

    emptied = await gather_or_cancel(*(empty(k) for k in page.orphans))
    await blobs.forget_uploads(
        [k for k, is_empty in zip(page.orphans, emptied, strict=True) if is_empty], min_age_s
    )


async def run_pass() -> BlobGcResult:
    min_age = int(settings.document_blob_min_age_seconds)
    deleted = 0
    touched = 0
    with record_pass("document_blob_gc") as rec:
        # The bucket walk first: it is bounded (its pages), the namespace walk is
        # not, and a pass cut at its deadline must not starve the walk for good.
        swept = await _sweep_bucket(min_age) if settings.document_s3_bucket else 0
        after = ("", "")
        while True:
            page = await blobs.namespaces(after, _NAMESPACES)
            for org, ns in page:
                n = await sweep_namespace(org, ns, min_age)
                deleted += n
                touched += 1 if n else 0
            if len(page) < _NAMESPACES:
                break
            after = page[-1]
        rec.outcome = {"deleted": deleted, "namespaces": touched, "objects_deleted": swept}
    if swept:
        log.info("document_blob_gc: deleted %d unnamed object(s)", swept)
    if deleted:
        log.info(
            "document_blob_gc: deleted %d unreferenced original(s) in %d namespace(s)",
            deleted,
            touched,
        )
    elif not swept:
        pass_context.mark_idle()
    return BlobGcResult(deleted=deleted, namespaces=touched, objects=swept)
