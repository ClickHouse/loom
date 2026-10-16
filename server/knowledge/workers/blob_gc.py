"""Document-original sweep: `loom.document_blobs` rows no live document names.

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

Erasure does not wait for this: its own step deletes every original of the
scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.knowledge.store import blobs
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Originals read per statement; at most _MAX_BATCHES of them per namespace per
# pass, and the next pass continues.
_BATCH = 2000
_MAX_BATCHES = 10
# Namespaces listed per read of the walk.
_NAMESPACES = 500


@dataclass(frozen=True)
class BlobGcResult:
    deleted: int  # originals deleted this pass
    namespaces: int  # (org, namespace) pairs they came from


async def sweep_namespace(org: str, namespace: str, min_age_s: int) -> int:
    """Delete the namespace's unreferenced originals; returns how many went. The
    sha cursor moves past every batch, so a row the delete spared is not read
    again this pass."""
    deleted, after = 0, ""
    for _ in range(_MAX_BATCHES):
        found = await blobs.unreferenced(org, namespace, min_age_s, _BATCH, after=after)
        if not found:
            break
        deleted += await blobs.delete(org, namespace, found, min_age_s=min_age_s)
        if len(found) < _BATCH:
            break
        after = found[-1]
    return deleted


async def run_pass() -> BlobGcResult:
    min_age = int(settings.document_blob_min_age_seconds)
    deleted = 0
    touched = 0
    with record_pass("document_blob_gc") as rec:
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
        rec.outcome = {"deleted": deleted, "namespaces": touched}
    if deleted:
        log.info(
            "document_blob_gc: deleted %d unreferenced original(s) in %d namespace(s)",
            deleted,
            touched,
        )
    else:
        pass_context.mark_idle()
    return BlobGcResult(deleted=deleted, namespaces=touched)
