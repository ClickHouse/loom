"""Postgres delivery worker: publishes committed revisions a request did not.

Each pass takes the deliveries whose writer's lease ran out before it published
them (`server.memory.app.pg_delivery`), then sweeps what has served its time:
idempotency keys past their retention, erased memories' ids past
LOOM_PG_ERASURE_EVIDENCE_HOURS, and settled deliveries past the insert bound and
LOOM_PG_REVISION_PAYLOAD_MINUTES, with their revisions' payloads
(`pg_memory.sweep_settled`); last, it publishes the
audit rows waiting in the outbox (rows an erasure moved there, or those a claim deferred
because the holder it replaced may still insert them). Idle under the ClickHouse authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.core import pg
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    claimed: int = 0
    published: int = 0
    # Failed publish attempts counted on their deliveries.
    failed: int = 0
    # Idempotency keys past their retention, deleted this pass.
    expired_keys: int = 0
    # Erased memories' ids past LOOM_PG_ERASURE_EVIDENCE_HOURS.
    expired_evidence: int = 0
    # Settled deliveries past the insert bound and LOOM_PG_REVISION_PAYLOAD_MINUTES.
    swept_settled: int = 0
    # Their revisions' payloads cleared with them.
    cleared_payloads: int = 0
    # Audit rows published from the outbox (rows an erasure moved there, or those a
    # claim deferred because the holder it replaced may still insert them).
    published_audits: int = 0


async def run_pass() -> DeliveryResult:
    from server.core import idempotency
    from server.memory.app.pg_delivery import publish_audit_outbox, publish_claims
    from server.memory.store import pg_memory

    result = DeliveryResult()
    if not pg.enabled():
        pass_context.mark_idle()
        return result
    with record_pass("pg_delivery") as rec:
        claims = await pg_memory.claim_due(settings.pg_delivery_batch_rows)
        result.claimed = len(claims)
        if claims:
            result.published, result.failed = await publish_claims(claims)
        result.expired_keys = await idempotency.sweep_expired(settings.pg_delivery_batch_rows)
        result.expired_evidence = await pg_memory.sweep_erasure_evidence(
            settings.pg_delivery_batch_rows
        )
        result.swept_settled, result.cleared_payloads = await pg_memory.sweep_settled(
            settings.pg_delivery_batch_rows
        )
        result.published_audits = await publish_audit_outbox(settings.pg_delivery_batch_rows)
        rec.outcome = {
            "claimed": result.claimed,
            "published": result.published,
            "failed": result.failed,
            "expired_keys": result.expired_keys,
            "expired_evidence": result.expired_evidence,
            "swept_settled": result.swept_settled,
            "cleared_payloads": result.cleared_payloads,
            "published_audits": result.published_audits,
        }
        swept = (
            result.expired_keys
            + result.expired_evidence
            + result.swept_settled
            + result.cleared_payloads
        )
        if not claims and not swept and not result.published_audits:
            pass_context.mark_idle()
        elif result.published or result.published_audits:
            log.info(
                "pg_delivery: published %d revisions, %d outbox audit rows",
                result.published,
                result.published_audits,
            )
    return result
