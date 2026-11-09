"""Publication of committed knowledge documents a request did not publish, under the
Postgres authority: the delivery worker (`server.knowledge.workers.pg_delivery`) takes
the deliveries whose writer's lease ran out and inserts their loom.documents rows.

loom.documents is ReplacingMergeTree(version), so a row the writer did insert before
its lease ran out collapses into this copy. A delivery that keeps failing is parked
after LOOM_PG_DELIVERY_MAX_ATTEMPTS refusals, with its last error, and holds back only
itself.
"""

from __future__ import annotations

import logging

from server.core import ch_errors, pg_lease
from server.core.error_summary import error_summary
from server.core.pg_lease import PublicationPending
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import pg_documents
from server.settings import settings

log = logging.getLogger(__name__)


async def publish_due(limit: int) -> tuple[int, int, int]:
    """Deliveries claimed, published and refused this pass, each published under the
    claims' lease, one at a time: a delivery's retirement is its own (stamped at its
    tombstone's `superseded_at`, which restore matches, and bounded by its own version; a
    delivery without the retirement bit retires nothing), which one insert for several
    deliveries of a namespace cannot keep.
    ClickHouse unavailable (or in a state that clears by itself), or the lease ending
    before a publish starts (`pg_lease.leased` refuses on entry, before any send),
    refuses nothing: the pass stops and releases what it did not publish, counting no
    attempt. A send the lease's end cut off is refused by its start guard as a late send
    while ClickHouse answers, and counts, so one that always outlasts the lease parks."""
    claims, deadline = await pg_documents.claim_due(limit)
    published = failed = 0
    for i, d in enumerate(claims):
        try:
            older = {d.source_uri} if d.targets & pg_documents.RETIRE_OLDER else frozenset()
            with pg_lease.leased(deadline):
                await documents_repo.publish([d.row], retire_older=older)
        except Exception as e:  # noqa: BLE001 — one delivery's failure holds back only itself
            cause = documents_repo.OlderNotRetired.unwrap(e)
            if isinstance(e, PublicationPending) or await ch_errors.refused_nothing(
                cause, probe=True
            ):
                log.warning("knowledge_pg_delivery: pass stopped (%s)", error_summary(cause))
                await pg_documents.release(claims[i:])
                break
            failed += 1
            await _count_failure(d, cause)
            continue
        await pg_documents.complete(d.org, d.namespace, d.source_uri, d.version, token=d.token)
        published += 1
    return len(claims), published, failed


async def _count_failure(d: pg_documents.Delivery, cause: BaseException) -> None:
    """One attempt of `d` counted, and parked at LOOM_PG_DELIVERY_MAX_ATTEMPTS."""
    park = d.attempts + 1 >= settings.pg_delivery_max_attempts
    if park:
        log.warning(
            "knowledge delivery %s/%s %s v%d parked after %d attempts: %s",
            d.org,
            d.namespace,
            d.source_uri,
            d.version,
            d.attempts + 1,
            error_summary(cause),
        )
    await pg_documents.failed(d, error_summary(cause), park=park)
