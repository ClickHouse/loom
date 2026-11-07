"""memory.search_trace (the per-candidate scoring breakdown) and
memory.search_trace_log (the recorded per-query telemetry)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from server.core import pg
from server.core.ch import get_client, sequential_reads
from server.core.naming import named
from server.embedding import get_embedder
from server.memory.api.models_search import (
    MemorySearchTraceInput,
    MemorySearchTraceOutput,
    SearchTraceLogInput,
    SearchTraceLogOutput,
    _MemorySearchHit,
    _SearchTraceHit,
    _SearchTraceLogRow,
)
from server.memory.app.search import pg_validation
from server.memory.app.search.embed import _bounded_query_embed
from server.memory.app.search.filters import read_filters
from server.memory.app.search.query_spelling import embed_query
from server.memory.app.search.vector_legs import _plane_fields, _stamp_row_fields


async def handle_memory_search_trace(input: MemorySearchTraceInput) -> Any:
    """Same shape as memory.search; returns the vector leg's per-candidate scoring
    breakdown (entity_boost + rank_score as separate columns; no fusion, assembly or
    reranker).
    """
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import pin_read_filters
    from server.memory.app.retrieval import SearchQuery, vector_search_trace
    from server.observability.otel import traced

    input, _ = pin_read_filters(input, current_auth())
    input = input.model_copy(update={"run_id": named(input.run_id)})
    embedder = get_embedder()
    with traced("embed.compute.query", model=embedder.model_name):
        query_vec, spellings = await embed_query(
            lambda text: _bounded_query_embed(embedder, text),
            input.query,
            input,
            embedder.model_name,
        )

    entity_filter_lower = [t.lower() for t in (input.entity_filter or [])]

    # At the barrier a search waits at, read sequentially after a wait, as a search does.
    sequential = await pg_validation.admit(input.org, [input.namespace])
    with (
        traced("ch.query.memory_embeddings", op="search_trace"),
        sequential_reads() if sequential else nullcontext(),
    ):
        hits = await vector_search_trace(
            SearchQuery(
                org=input.org,
                namespace=input.namespace,
                **read_filters(input),
                embedding_model=embedder.model_name,
                query_vec=query_vec,
                top_k=input.top_k,
                entity_filter=list(input.entity_filter or []),
            )
        )

    served = await _served(input, hits)
    results: list[_SearchTraceHit] = []
    for i, h in enumerate(served, start=1):
        results.append(
            _SearchTraceHit(
                memory_key=h.memory_key,
                memory_type=h.memory_type,
                content_excerpt=h.content_excerpt,
                distance=h.distance,
                similarity=1.0 - h.distance,
                entity_boost=h.entity_boost,
                rank_score=h.rank_score,
                rank=i,
                **_plane_fields(h),
            )
        )
    return MemorySearchTraceOutput(
        results=results,
        embedding_model=embedder.model_name,
        entity_filter=entity_filter_lower,
        query_spellings=spellings or {},
        query_spellings_skipped=spellings is None,
        stale_dropped=len(hits) - len(served),
    )


async def _served(input: MemorySearchTraceInput, hits: list[Any]) -> list[Any]:
    """The hits the Postgres authority still serves, checked as memory.search checks its
    own (`pg_validation`); all of them under the ClickHouse authority. The probes carry
    only what the check reads: source, namespace, key, type, version and identity. A
    replay (`as_of`) is served as it read, as memory.search serves one."""
    if not pg.enabled() or input.as_of is not None:
        return hits
    probes = [
        _stamp_row_fields(
            h,
            _MemorySearchHit(
                memory_key=h.memory_key,
                memory_type=h.memory_type,
                content_excerpt="",
                distance=h.distance,
                similarity=1.0 - h.distance,
                version=h.version,
            ),
        )
        for h in hits
    ]
    stale = await pg_validation.stale_flags(input.org, input.namespace, probes)
    return [h for h, s in zip(hits, stale, strict=True) if not s]


async def _reader_actors(org: str, me: str) -> list[str]:
    """Every actor spelling of the caller's principal: `me`, plus the same kind over each
    spelling the directory ties to the subject (login handle and verified email,
    auth.aliases — the rule grants fold by), so one person reads one history."""
    if not me:
        return []
    from server.auth.aliases import subject_aliases
    from server.auth.context import current_auth
    from server.memory.domain.trust import actor_for

    auth = current_auth()
    subject = str(getattr(auth, "subject", "") or "") if auth is not None else ""
    if not subject:
        return [me]
    sa = bool(auth.is_service_account) if auth is not None else False
    spellings = await subject_aliases(org, subject)
    return list(dict.fromkeys([me, *(actor_for(s, is_service_account=sa) for s in spellings)]))


async def handle_memory_search_trace_log(input: SearchTraceLogInput) -> SearchTraceLogOutput:
    """Read the per-query recall telemetry (loom.search_trace_log) for a
    namespace: query text, returned keys+scores, abstain/risk, latency. Lets a
    caller correlate recalled memories to downstream outcomes over the wire,
    without direct ClickHouse access. Requires LOOM_SEARCH_TRACE_LOG_ENABLED
    (else the table is empty)."""
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "ts > now() - toIntervalHour({h:UInt32})",
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "h": int(input.since_hours),
        "lim": int(input.limit),
    }
    # A namespace admin reads every row; anyone else only the rows recorded for its own
    # server-stamped principal (the body's `agent` is a label anyone can name). Rows
    # written before the actor was stamped carry '' and are an admin's only.
    from server.auth.authz import row_in_scope
    from server.memory.app.provenance import writer_actor
    from server.operations.base import Role

    if not await row_in_scope(input.org, input.namespace, Role.ADMIN):
        actors = await _reader_actors(input.org, writer_actor())
        if not actors:
            return SearchTraceLogOutput(rows=[])
        where.append("actor IN {actors:Array(String)}")
        params["actors"] = actors
    if named(input.agent, wildcard=True):
        where.append("agent = {agent:String}")
        params["agent"] = input.agent
    if named(input.run_id):
        where.append("run_id = {rid:String}")
        params["rid"] = input.run_id
    rows = await get_client().query_all(
        f"""
        SELECT ts, agent, query, returned_keys, returned_scores, top_k,
               result_count, abstain, retrieval_risk, embedding_model,
               latency_ms, run_id, operation
        FROM loom.search_trace_log
        WHERE {" AND ".join(where)}
        ORDER BY ts DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return SearchTraceLogOutput(
        rows=[
            _SearchTraceLogRow(
                ts=r["ts"],
                agent=str(r.get("agent") or ""),
                query=str(r.get("query") or ""),
                returned_keys=list(r.get("returned_keys") or []),
                returned_scores=[float(x) for x in (r.get("returned_scores") or [])],
                top_k=int(r.get("top_k") or 0),
                result_count=int(r.get("result_count") or 0),
                abstain=bool(r.get("abstain")),
                retrieval_risk=str(r.get("retrieval_risk") or ""),
                embedding_model=str(r.get("embedding_model") or ""),
                latency_ms=int(r.get("latency_ms") or 0),
                run_id=str(r.get("run_id") or "-"),
                operation=str(r.get("operation") or ""),
            )
            for r in rows
        ]
    )
