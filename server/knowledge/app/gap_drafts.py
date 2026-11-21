"""One private draft from a current gap's currently eligible normalized questions."""

import time

from fastapi import HTTPException

from server.knowledge.api.models_gaps import KnowledgeGapsDraftInput, KnowledgeGapsDraftOutput
from server.knowledge.app import gaps
from server.knowledge.store import gaps as repo
from server.memory.app.definition_drafts import draft
from server.observability import capture_consent
from server.settings import settings


async def handle_gap_draft(input: KnowledgeGapsDraftInput) -> KnowledgeGapsDraftOutput:
    started = int(time.time())
    minimum = settings.knowledge_gaps_min_askers

    async def valid() -> bool:
        return (
            settings.knowledge_gaps_enabled
            and minimum == settings.knowledge_gaps_min_askers
            and await capture_consent.level_uncached(input.org, input.namespace) != "off"
            and not await gaps.erasure_since(input.org, input.namespace, started, None)
        )

    row = (
        await repo.gap_for_draft(input.org, input.namespace, input.gap_id)
        if await valid()
        else None
    )
    queries = (
        await repo.draft_queries(
            input.org,
            input.namespace,
            [row["head"], *row["samples"]],
            minimum,
        )
        if row
        else []
    )
    if not queries:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    cid = await draft(
        input.org,
        input.namespace,
        kind="knowledge_gap",
        resource_id=input.gap_id,
        evidence={"queries": queries},
        supporters=minimum,
        still_valid=valid,
    )
    return KnowledgeGapsDraftOutput(candidate_id=cid)
