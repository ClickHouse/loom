"""search: the loom_search fan-out as a registry operation (wire models in
server/memory/api/models_loom_search.py; the legs and assembly in memory/app/loom_search)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext, suppress
from typing import Any

from server.code.app.repo_scope import repo_namespace_in_scope
from server.core.diagnostics import private_work
from server.core.request_work import mirror_marked
from server.knowledge.app import live_search
from server.memory.api.models_loom_search import (
    LiveSearchHit,
    LiveSearchResults,
    SearchInput,
    SearchOutput,
)
from server.memory.app.loom_search.fanout import search_sections
from server.memory.app.loom_search.tracker_leg import prepare_tracker_search

log = logging.getLogger(__name__)


async def handle_search(input: SearchInput) -> SearchOutput:
    """The sections for `input.query`, addressed to the namespace the
    transport authorized. An `all_mine` search also reads the caller's
    memory-kind namespaces; when they cannot be resolved it narrows to
    `namespace`, never widens. Prompt text is captured at the most
    restrictive consent of every namespace read, the code scope's repository
    namespace and every planned tracker namespace included."""
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
    tracker_plan = await prepare_tracker_search(input.query, org=input.org, top_k=input.top_k)
    consent_namespaces = [*read, repo_namespace] if repo_namespace else list(read)
    if tracker_plan and tracker_plan.org == input.org:
        consent_namespaces.extend(
            ns for ns in tracker_plan.namespaces if ns not in consent_namespaces
        )
    await narrow_to(input.org, consent_namespaces)
    if tracker_plan and tracker_plan.org != input.org:
        await narrow_to(tracker_plan.org, list(tracker_plan.namespaces))
    live_task = None
    if input.live and not input.page.strip() and not input.tags:
        live_task = asyncio.create_task(
            live_search.search(
                input.org,
                [*read, repo_namespace] if repo_namespace else list(read),
                input.query,
                top_k=input.top_k,
            ),
            name="search.query.live",
        )
    try:
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
            tracker_plan=tracker_plan,
        )
        result = await live_task if live_task is not None else None
        with private_work() if mirror_marked() else nullcontext():
            result = await live_search.publish(result) if result is not None else None
            if result is not None:
                payload["live"] = LiveSearchResults(
                    results=[
                        LiveSearchHit(
                            id=hit.id,
                            title=hit.title,
                            url=hit.url,
                            provider=hit.provider,
                            content=hit.content if input.bodies != "none" else "",
                        )
                        for hit in result.hits
                    ]
                )
            _record_gap_miss(input, payload, repo_namespace or "")
            return SearchOutput.model_validate(payload)
    finally:
        if live_task is not None and not live_task.done():
            live_task.cancel()
            with suppress(asyncio.CancelledError):
                await live_task


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
    if payload.get("live"):
        return  # a live answer must not become a persisted unanswered-query contribution
    if input.page.strip() or input.tags:
        return  # a later page was judged with the first; tags narrow the search
    withheld = payload.get("withheld") or {}
    if withheld.get("reason") in _ANSWER_LED:
        return  # an answer led and withheld the rest
    if payload.get("tracker") or payload.get("namespaces_skipped"):
        return  # tracker evidence answered, or the fan-out stopped short
    memory = payload.get("memory") or {}
    others = [payload.get(k) or {} for k in ("knowledge", "profile", "conversations")]
    if any(s.get("error") for s in (memory, *others)) or any(s.get("results") for s in others):
        return
    hits = memory.get("results") or []
    if memory.get("term_leg") not in ("complete", "empty") or any(h.get("governed") for h in hits):
        return  # a governed definition answers, or a term leg not complete or empty may hold one
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
