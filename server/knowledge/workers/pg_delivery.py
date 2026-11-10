"""Knowledge delivery worker: publishes the knowledge documents a request committed to
the Postgres authority but did not publish to ClickHouse
(`server.knowledge.app.pg_delivery`), and sweeps expired deletion evidence
(`loom.erased_documents`), settled deliveries and the ingestion jobs no run has saved for
30 days (`loom.knowledge_jobs`, as loom.knowledge_ingestions' TTL drops them), whether or
not the ingest worker runs. Idle under the ClickHouse authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.core import pg
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass


@dataclass
class KnowledgeDeliveryResult:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    expired_evidence: int = 0
    swept_settled: int = 0
    expired_jobs: int = 0


async def run_pass() -> KnowledgeDeliveryResult:
    from server.knowledge.app.pg_delivery import publish_due
    from server.knowledge.store import pg_documents, pg_jobs

    result = KnowledgeDeliveryResult()
    if not pg.enabled():
        pass_context.mark_idle()
        return result
    with record_pass("knowledge_pg_delivery") as rec:
        result.claimed, result.published, result.failed = await publish_due(
            settings.pg_delivery_batch_rows
        )
        result.expired_evidence = await pg_documents.sweep_erasure_evidence(
            settings.pg_delivery_batch_rows
        )
        result.swept_settled = await pg_documents.sweep_settled(settings.pg_delivery_batch_rows)
        result.expired_jobs = await pg_jobs.expire(settings.pg_delivery_batch_rows)
        rec.outcome = {
            "claimed": result.claimed,
            "published": result.published,
            "failed": result.failed,
            "expired_evidence": result.expired_evidence,
            "swept_settled": result.swept_settled,
            "expired_jobs": result.expired_jobs,
        }
    if not (
        result.claimed or result.expired_evidence or result.swept_settled or result.expired_jobs
    ):
        pass_context.mark_idle()
    return result
