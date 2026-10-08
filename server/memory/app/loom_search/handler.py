"""search: the loom_search fan-out as a registry operation (wire models in
server/memory/api/models_loom_search.py; the legs and assembly in memory/app/loom_search)."""

from __future__ import annotations

import logging

from server.memory.api.models_loom_search import SearchInput, SearchOutput
from server.memory.app.loom_search.fanout import search_sections

log = logging.getLogger(__name__)


async def handle_search(input: SearchInput) -> SearchOutput:
    """The sections for `input.query`, addressed to the namespace the
    transport authorized. An `all_mine` search also reads the caller's
    memory-kind namespaces; when they cannot be resolved it narrows to
    `namespace`, never widens. Prompt text is captured at the most
    restrictive consent of every namespace read."""
    from server.auth.context import resolve_fanout_namespaces
    from server.observability.capture_consent import narrow_to

    fanout: list[str] = []
    if input.namespace_scope == "all_mine":
        try:
            fanout = await resolve_fanout_namespaces(input.org)
        except Exception as e:  # noqa: BLE001 — no fan-out namespaces narrows the search, never widens
            log.warning("search namespace resolution failed (non-fatal): %s", e)
    await narrow_to(input.org, [input.namespace, *fanout])
    payload = await search_sections(
        org=input.org,
        namespace=input.namespace,
        query=input.query,
        top_k=input.top_k,
        tags=[t for t in input.tags if t],
        namespace_scope=input.namespace_scope,
        recipe=input.recipe,
        bodies=input.bodies,
        page=input.page.strip(),
        explain=input.explain,
        fanout_namespaces=fanout,
    )
    return SearchOutput.model_validate(payload)
