"""Entity-communities worker (default-off).

Supervisor adapter around `server.memory.app.entity_communities`: one pass
recomputes the co-occurrence graph's communities for every selected
namespace (the consolidation opt-ins plus the namespaces whose graph was
walked recently) and writes them to
loom.entity_communities. No LLM; the cost is the edge scan plus label
propagation in Python, which runs in a worker thread (asyncio.to_thread) so
the event loop stays free. An oversized namespace keeps its strongest
`max_edges` pairs instead of stalling the pass. Enable with LOOM_ENTITY_COMMUNITIES_ENABLED=true.
"""

from __future__ import annotations

import logging

from server.memory.app.entity_communities import (
    EntityCommunitiesConfig,
    EntityCommunitiesResult,
    run_entity_communities,
)
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)


async def run_pass() -> EntityCommunitiesResult:
    cfg = EntityCommunitiesConfig(
        max_edges_per_namespace=int(settings.entity_communities_max_edges),
        min_co_occur=int(settings.entity_communities_min_co_occur),
        walked_days=int(settings.entity_communities_walked_days),
        hub_drop_fraction=float(settings.entity_communities_hub_drop_fraction),
        giant_share=float(settings.entity_communities_giant_share),
        hub_drop_max_fraction=float(settings.entity_communities_hub_drop_max_fraction),
    )
    with record_pass("entity_communities") as rec:
        # The CH reads/writes are async; the CPU-bound partition of each
        # namespace runs in a thread inside run_entity_communities.
        result = await run_entity_communities(cfg)
        rec.outcome = {
            "namespaces": result.namespaces,
            "rows_written": result.rows_written,
            "truncated": len(result.truncated),
            "hub_escalated": len(result.hub_fraction_used),
            "giant": len(result.giant),
        }
    log.info(
        "entity communities pass: namespaces=%d rows=%d truncated=%d escalated=%d giant=%d",
        result.namespaces,
        result.rows_written,
        len(result.truncated),
        len(result.hub_fraction_used),
        len(result.giant),
    )
    return result
