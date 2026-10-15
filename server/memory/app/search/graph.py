"""Graph mode: seeding and walking the entity graph, relation-typed triple
matching, the intent-weighted graph policy and the graph evidence counts."""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.ch import get_client
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scope import _SearchRun


def _graph_seed_mode(input: Any) -> str:
    """query | recent | both — the request's, else the deployment's."""
    from server.settings import settings as _s

    mode = str(getattr(input, "graph_seed", "") or "") or str(_s.memory_search_graph_seed)
    return mode if mode in ("query", "recent", "both") else "query"


async def _recent_graph_seeds(ctx: SearchContext, ch: Any) -> list[str]:
    """Entities of the identity's newest memories, for graph_seed recent|both;
    [] under query (the default) or on any failure."""
    input = ctx.input
    if _graph_seed_mode(input) == "query":
        return []
    from server.memory.app.recent_seeds import recent_seed_entities
    from server.settings import settings as _s

    sid = str(getattr(input, "session_id", "") or "")
    uid = str(getattr(input, "user_id", "") or "")
    return await recent_seed_entities(
        org=input.org,
        namespace=input.namespace,
        agent=str(getattr(input, "agent", "") or "") or None,
        user_id=uid if uid and uid != "-" else None,
        session_id=sid if sid and sid != "-" else None,
        memories=int(_s.memory_search_graph_seed_recent_memories),
        limit=int(_s.memory_search_graph_seed_recent_max),
        ch=ch,
    )


async def _run_graph_expansion(ctx: SearchContext) -> None:
    """Graph mode: pre-expand query entities 1-hop via loom.entity_edges
    before the cosine search; the expanded set feeds vector_search as a
    graph-overlap boost. Per namespace (edges are namespace-local).
    Fail-open: an empty expansion degrades to entity-boosted semantic."""
    input = ctx.input
    if input.search_mode != "graph":
        return

    _ch = get_client()
    # Seed the walk from what was said last as well. The entities of the identity's newest
    # memories join the query's entities as seeds for the expansion only — never as the entity
    # FILTER, which would narrow the legs to whatever the last turn mentioned.
    recent_seeds = await _recent_graph_seeds(ctx, _ch)
    seeds = list(ctx.effective_entity_filter)
    for e in recent_seeds:
        if e not in seeds:
            seeds.append(e)
    if recent_seeds:
        ctx.actual_work["graph_recent_seeds"] = len(recent_seeds)
    if not seeds:
        return
    from server.memory.app.retrieval import fetch_graph_neighbors

    graph_query_date = ctx.query_anchor_iso or ctx.obs_date or None

    async def _neighbors_for(ns: str) -> list[str]:
        return await fetch_graph_neighbors(
            _ch,
            org=input.org,
            namespace=ns,
            seed_entities=seeds,
            # Route per neighbour, so a hit can show its path.
            paths=ctx.graph_paths_by_ns.setdefault(ns, {}),
            min_co_occur=1,
            max_neighbors=50,
            # Cross-namespace expansion: the entity hop unions across the
            # namespaces this search reads (`ctx.namespaces`: the addressed
            # one, plus the all_mine fan-out under its cap), so always on.
            accessible_namespaces=ctx.namespaces,
            # Edge-level temporal validity: replay `as_of` wins, else the
            # resolved query/observation date. NULL bounds always-valid.
            query_date=graph_query_date,
        )

    # Per-namespace neighbor expansions are independent CH reads;
    # gather concurrently. Result map is order-independent.
    _neighbor_lists = await asyncio.gather(*[_neighbors_for(ns) for ns in ctx.namespaces])
    ctx.graph_expanded_by_ns = dict(zip(ctx.namespaces, _neighbor_lists, strict=True))


def _graph_via_path(ctx: SearchContext, ns: str, hit: Any) -> list[str]:
    """Graph mode only: the chain from a query entity to one of the hit's entities. [] in
    every other mode and for hits that are in the pool for a non-graph reason."""
    if getattr(ctx.input, "search_mode", "") != "graph":
        return []
    ents = list(getattr(hit, "entities", None) or [])
    if not ents:
        return []
    from server.memory.domain.graph_paths import via_path

    return via_path(ents, ctx.effective_entity_filter, ctx.graph_paths_by_ns.get(ns, {}))


async def _run_graph_stages(ctx: SearchContext) -> None:
    """The two graph-mode stages that precede the vector legs, run concurrently.

    `_run_graph_expansion` seeds and walks the entity graph (`effective_entity_filter`,
    `graph_expanded_by_ns`); `_run_triple_matching` extracts the query's triples with one LLM
    call and looks up the memories whose stored triples match (`query_triples`,
    `triple_matched_by_ns`). They share the input and the namespaces and write disjoint fields,
    so the stage costs the longer of the two instead of their sum: in measured traces the
    triple call is ~566 ms at p50 and runs on every graph-mode search. Both stages return early
    when the mode is not graph, so the gather is free elsewhere."""
    await asyncio.gather(_run_graph_expansion(ctx), _run_triple_matching(ctx))


async def _run_triple_matching(ctx: SearchContext) -> None:
    """Graph mode relation typing. Extract (subject, verb, object) triples
    from the query via LLM, then look up memory_keys whose stored triples
    match. Matches get a rank_score boost above graph_overlap and
    entity_boost — typed relations are higher-signal than co-occurrence."""
    input = ctx.input
    if input.search_mode != "graph":
        return
    from server.memory.app.search_llm import extract_query_triples_llm

    ctx.actual_work["llm_graph_triple_calls"] = 1
    ctx.query_triples = await extract_query_triples_llm(
        input.query, on_llm_call=ctx.search_budget.note_llm_call
    )
    if not ctx.query_triples:
        return
    from server.core.ch import get_client as _get_ch_t
    from server.memory.app.retrieval import fetch_triple_matched_memories

    _ch_t = _get_ch_t()
    for ns in ctx.namespaces:
        matched = await fetch_triple_matched_memories(
            _ch_t,
            org=input.org,
            namespace=ns,
            query_triples=ctx.query_triples,
            max_keys=50,
            expand_synonyms=True,
            on_llm_call=ctx.search_budget.note_llm_call,
        )
        ctx.triple_matched_by_ns[ns] = matched


def _count_graph_evidence(ctx: SearchContext, run: _SearchRun) -> None:
    """Phase 16b: the graph evidence counts every response reports."""
    input = ctx.input
    run.graph_seeds = (
        len(ctx.effective_entity_filter)
        if input.search_mode == "graph" and ctx.effective_entity_filter
        else 0
    )
    run.graph_neighbors = sum(len(v) for v in ctx.graph_expanded_by_ns.values())
    run.triples_matched = sum(len(v) for v in ctx.triple_matched_by_ns.values())
