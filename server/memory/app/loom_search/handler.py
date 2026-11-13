"""search: the loom_search fan-out as a registry operation (wire models in
server/memory/api/models_loom_search.py; the legs and assembly in memory/app/loom_search)."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.repo_scope import repo_namespace_in_scope
from server.memory.api.models_loom_search import SearchInput, SearchOutput
from server.memory.app.loom_search.fanout import search_sections

log = logging.getLogger(__name__)


async def handle_search(input: SearchInput) -> SearchOutput:
    """The sections for `input.query`, addressed to the namespace the
    transport authorized. An `all_mine` search also reads the caller's
    memory-kind namespaces; when they cannot be resolved it narrows to
    `namespace`, never widens. Prompt text is captured at the most
    restrictive consent of every namespace read, the code scope's repository
    namespace included."""
    from server.auth.fanout import namespaces_to_search
    from server.observability.capture_consent import narrow_to

    namespace_scope = input.namespace_scope
    try:
        read, _ = await namespaces_to_search(input.org, input.namespace, namespace_scope)
    except Exception as e:  # noqa: BLE001 — no fan-out namespaces narrows the search, never widens
        log.warning("search namespace resolution failed (non-fatal): %s", e)
        # Consent is narrowed below over the addressed namespace (plus the code
        # scope's repository namespace), so no leg reads wider than its consent.
        read, namespace_scope = [input.namespace], "this"
    # Under a code scope the repository's documents and decisions are read too (on a later
    # page, its documents only).
    repo_namespace = await repo_namespace_in_scope(input.org)
    await narrow_to(input.org, [*read, repo_namespace] if repo_namespace else read)
    payload = await search_sections(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        query=input.query,
        top_k=input.top_k,
        tags=[t for t in input.tags if t],
        namespace_scope=namespace_scope,
        recipe=input.recipe,
        bodies=input.bodies,
        page=input.page.strip(),
        explain=input.explain,
        repo_namespace=repo_namespace,
    )
    _record_gap_miss(input, payload, repo_namespace or "")
    return SearchOutput.model_validate(payload)


# The `withheld` reasons that mean an answer led and the rest was set aside.
_ANSWER_LED = frozenset({"home_fact_leads", "tracker_verdict_leads"})


def _record_gap_miss(input: SearchInput, payload: dict[str, Any], repo_namespace: str) -> None:
    """A knowledge-gap miss: the memory section missed as `memory.search` does or had
    every hit fall below the precision floor, every other section came back empty, and
    nothing failed, led or narrowed the search."""
    from server.knowledge.app.gaps import reads_whole_namespace, record_miss
    from server.knowledge.domain.gaps import memory_missed, unsettled
    from server.settings import settings

    if not settings.knowledge_gaps_enabled:
        return
    if input.page.strip() or input.tags:
        return  # a later page was judged with the first; tags narrow the search
    withheld = payload.get("withheld") or {}
    if withheld.get("reason") in _ANSWER_LED:
        return  # an answer led and withheld the rest
    if payload.get("tracker") or payload.get("namespaces_skipped"):
        return  # a tracker verdict answered, or the fan-out stopped short
    memory = payload.get("memory") or {}
    others = [payload.get(k) or {} for k in ("knowledge", "profile", "conversations")]
    if any(s.get("error") for s in (memory, *others)) or any(s.get("results") for s in others):
        return
    hits = memory.get("results") or []
    if repo_namespace and repo_namespace != input.namespace:
        if any(h.get("namespace") == repo_namespace for h in hits):
            return  # a decision merged from the scoped repository answers
    if hits and not memory_missed(
        bool(memory.get("abstain")), str(memory.get("retrieval_risk") or "")
    ):
        return
    if any(
        unsettled(
            degraded=s.get("degraded"),
            pending_extraction=int(s.get("pending_extraction") or 0),
            stale_dropped=int(s.get("stale_dropped") or 0),
        )
        for s in (memory, *others)
    ):
        return
    if not reads_whole_namespace():
        return
    record_miss(
        op="search.query",
        org=input.org,
        namespace=input.namespace,
        query=input.query,
    )
