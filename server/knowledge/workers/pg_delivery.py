"""Knowledge delivery worker: publishes the knowledge documents a request committed to
the Postgres authority but did not publish to ClickHouse
(`server.knowledge.app.pg_delivery`), and sweeps expired deletion evidence
(`loom.erased_documents`), settled deliveries and the ingestion jobs no run has saved for
30 days (`loom.knowledge_jobs`, as loom.knowledge_ingestions' TTL drops them), whether or
not the ingest worker runs. Idle under the ClickHouse authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.core import pg
from server.core.error_summary import error_summary
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


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
        await _read_backlog()
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


async def _read_backlog() -> None:
    """The knowledge backlog for the pipeline gauges, first in the pass so a pass that
    fails on ClickHouse still takes it."""
    from server.knowledge.store import pg_documents
    from server.observability import pg_pipeline

    try:
        pg_pipeline.set_backlog(
            "knowledge", await pg_documents.backlog(settings.pg_backlog_count_cap)
        )
    except Exception as e:  # noqa: BLE001 — the pass's publishes must not wait on a gauge
        log.warning("knowledge_pg_delivery: backlog not read: %s", error_summary(e))
