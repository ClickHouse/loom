"""knowledge.reads — how often agents opened each document of a namespace.

The one per-document signal loom records is a read: every `knowledge.get`
leaves an `agent_events` row whose allow-listed `args['source_uri']` names
the document (event_args). What a search *returned* is not recorded for
knowledge, so this op says "opened", never "found". The dashboard's own
previews (`consumer: loom-ui`) and the assistant toolset (`loom-assistant`) are
left out, so a person checking a document does not count as an agent reading
it; bench traffic is left out too. `agent_events` is ordered by (org,
namespace, agent, ts): the read is a primary-key range on the namespace and
the window.
"""

from __future__ import annotations

from server.knowledge.api.models_knowledge import KnowledgeReadsInput, KnowledgeReadsOutput
from server.observability.consumer import INSPECTOR_CONSUMERS


async def handle_knowledge_reads(input: KnowledgeReadsInput) -> KnowledgeReadsOutput:
    from server.knowledge.store import documents as documents_repo

    days = max(1, min(int(input.days), 30))
    uris = [str(u) for u in (input.source_uris or []) if str(u)][:200]
    rows = await documents_repo.agent_opens(
        input.org, input.namespace, days=days, skip_consumers=INSPECTOR_CONSUMERS, source_uris=uris
    )
    return KnowledgeReadsOutput(
        window_days=days,
        reads=[
            {
                "source_uri": str(r.get("source_uri") or ""),
                "reads": int(r.get("reads") or 0),
                "agents": int(r.get("agents") or 0),
                "last_read_at": str(r.get("last_read_at") or ""),
            }
            for r in rows
        ],
    )
