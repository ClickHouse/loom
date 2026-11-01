"""`vector_search` and `vector_search_trace`: the phases of one memory.search
read over a namespace. Mode and plane mask, rank terms, the fact leg and its
side legs run concurrently, then the hits are merged, fused and finished.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import Any

from server.core.repo_base import _default_client
from server.memory.app.retrieval.chunks import _search_memory_chunks
from server.memory.app.retrieval.fact_rank import (
    _fact_boost_terms,
    _fact_mode_rank_score,
    _fact_rank_tail,
    _personalize_fact_terms,
)
from server.memory.app.retrieval.fact_sql import _fact_leg_sql
from server.memory.app.retrieval.fusion import _rerank_fact_pool
from server.memory.app.retrieval.lexical import _bm25_precursor, compound_match
from server.memory.app.retrieval.pool import _finish_pool
from server.memory.app.retrieval.retry import (
    _query_all_retrying_empty_block,
    _spanned,
)
from server.memory.app.retrieval.side_legs import (
    _fetch_unmatched_candidates,
    _lexical_recall_fallback,
    _search_memory_records,
)
from server.memory.domain.search_parsing import _iso_date
from server.memory.domain.search_sql import _build_where, _plane_enabled
from server.memory.domain.search_types import (
    RowKey,
    SearchHit,
    SearchQuery,
    SearchTraceHit,
    _CHClient,
    hit_row_key,
    row_identity,
)

log = logging.getLogger("server.memory.app.retrieval")


def speaker_from_tags(tags: Any) -> str:
    """The `speaker:<name>` tag as a speaker name, "" when
    absent. First such tag wins; a fact cited to two speakers carries none."""
    for t in list(tags or []):
        st = str(t)
        if st.startswith("speaker:") and len(st) > 8:
            return st[8:].strip()
    return ""


def _split_gathered(
    gathered: Sequence[Any], has_chunks: bool, has_lexical: bool, has_records: bool
) -> tuple[list[SearchHit], list[SearchHit], list[SearchHit]]:
    """The optional legs' results, in the order the gather ran them."""
    chunk_hits: list[SearchHit] = []
    lexical_pool: list[SearchHit] = []
    record_hits: list[SearchHit] = []
    gi = 1
    if has_chunks:
        chunk_hits = gathered[gi]
        gi += 1
    if has_lexical:
        lexical_pool = gathered[gi]
        gi += 1
    if has_records:
        record_hits = gathered[gi]
    return chunk_hits, lexical_pool, record_hits


async def _fold_reinforcement_into_rows(
    ch: _CHClient,
    q: SearchQuery,
    rows: list[dict[str, Any]],
) -> int:
    """Read loom.memory_reinforcement for the rows' keys and re-score them.
    Never raises: a replica that cannot serve the read keeps the SQL's
    write-time base, the value an unfolded row carries."""
    global _reinforcement_read_failures
    from server.memory.app.reinforcement import apply_folded_reinforcement, fetch_reinforcement

    try:
        folded = await fetch_reinforcement(
            ch,
            org=q.org,
            namespace=q.namespace,
            memory_keys=[str(r.get("memory_key") or "") for r in rows],
        )
    except Exception as e:  # noqa: BLE001 — the fold is a boost, never the leg
        _reinforcement_read_failures += 1
        if _reinforcement_read_failures <= 3 or _reinforcement_read_failures % 100 == 0:
            log.warning(
                "memory_reinforcement read failed (%d so far); rows keep the write-time base: %s",
                _reinforcement_read_failures,
                str(e)[:200],
            )
        return 0
    changed = apply_folded_reinforcement(
        rows,
        folded,
        weight=float(q.reinforcement_weight or 0.0),
        half_life_days=float(q.reinforcement_half_life_days or 90.0),
        use_scale=float(q.reinforcement_half_life_use_scale),
    )
    return changed


_reinforcement_read_failures = 0


def _apply_plane_mask(q: SearchQuery) -> SearchQuery:
    """Phase 1 of `vector_search`: a mode whose plane the route mask turned
    off degrades to semantic, dropping the inputs only that mode reads."""
    from dataclasses import replace as _replace

    if q.execution_planes:
        lexical_mode = q.search_mode in {"lexical", "hybrid", "bm25_hybrid", "rrf"}
        if lexical_mode and not _plane_enabled(q, "lexical"):
            q = _replace(q, search_mode="semantic")
        elif q.search_mode == "graph" and not _plane_enabled(q, "graph"):
            q = _replace(
                q,
                search_mode="semantic",
                graph_expanded=[],
                triple_matched_keys=[],
            )
        elif q.search_mode == "temporal" and not _plane_enabled(q, "temporal"):
            q = _replace(q, search_mode="semantic", query_anchor_iso="")
    return q


def _fact_rows_task(
    ch: _CHClient, q: SearchQuery, params: dict[str, Any], fact_sql: str
) -> Awaitable[list[dict[str, Any]]]:
    """Phase 7 of `vector_search`: the fact leg's read as one awaitable, with
    the reinforcement fold riding inside."""
    fact_rows_task = _spanned(
        "ch.query.memory_embeddings.ann",
        _query_all_retrying_empty_block(ch, fact_sql, params),
    )

    # The usage fold's reinforcement lives in a side table;
    # read it for the candidates by key (one primary-key statement, ~25 KB,
    # 17 ms) and re-score the shortlist in place. It rides inside the fact
    # task so it overlaps the chunk leg instead of following the fan-out. The
    # candidate set cannot change — the SQL orders by distance — so this is
    # exactly the in-SQL rank with the folded value.
    fold_reinforcement = (q.reinforcement_weight or 0.0) > 0

    async def _fact_rows_with_fold() -> list[dict[str, Any]]:
        fact_rows: list[dict[str, Any]] = await fact_rows_task
        if fact_rows and fold_reinforcement:
            await _spanned(
                "ch.query.memory_reinforcement",
                _fold_reinforcement_into_rows(ch, q, fact_rows),
            )
        return fact_rows

    return _fact_rows_with_fold()


async def _gather_legs(
    ch: _CHClient,
    q: SearchQuery,
    params: dict[str, Any],
    rows_task: Awaitable[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[SearchHit], list[SearchHit], list[SearchHit]]:
    """Phase 8 of `vector_search`: run the fact leg beside the chunk, lexical
    and record legs, and return the fact rows plus each optional leg's hits."""
    # Gather every result-independent leg in parallel — facts, chunks, the
    # BM25-only lexical union, records. None consumes another's output (the
    # lexical dedup against the vector-leg keys runs post-gather), so sequencing would
    # only add latency. The plane mask decides which legs participate.
    # Each leg is its own `ch.query.*` span. The handler's
    # `ch.query.memory_embeddings` span covers the whole fan-out, which would
    # hide the chunk, lexical and record legs inside one number. Each optional leg
    # retries the fresh-part error per statement itself and fails open on
    # anything else, so none takes a retry wrapper here.
    chunks_task = (
        _spanned(
            "ch.query.memory_chunks",
            _search_memory_chunks(ch, q, params),
        )
        if _plane_enabled(q, "chunk")
        else None
    )
    lexical_task = (
        # Admit lexical hits the vector leg missed, dropping those semantically far
        # (0.9 distance = 0.1 similarity floor) as false friends. The rrf route
        # skips it: its fact leg already fuses a lexical rank, and the pool is
        # a second FINAL scan of the namespace's vectors.
        _spanned(
            "ch.query.memory_embeddings.lexical",
            _lexical_recall_fallback(ch, q, set(), limit=50, max_distance=0.9),
        )
        if _plane_enabled(q, "lexical") and q.search_mode != "rrf"
        else None
    )
    records_task = (
        _spanned(
            "ch.query.memory_records",
            _search_memory_records(ch, q, limit=max(int(q.top_k) * 2, 25)),
        )
        if _plane_enabled(q, "record")
        else None
    )
    optional_tasks = [t for t in (chunks_task, lexical_task, records_task) if t is not None]
    gathered = await asyncio.gather(rows_task, *optional_tasks)
    rows = gathered[0]
    chunk_hits, lexical_pool, record_hits = _split_gathered(
        gathered, chunks_task is not None, lexical_task is not None, records_task is not None
    )
    return rows, chunk_hits, lexical_pool, record_hits


def _fact_rows_to_hits(
    rows: list[dict[str, Any]], q: SearchQuery, lexical_score_expr: str
) -> list[SearchHit]:
    """Phase 9 of `vector_search`: the fact rows as typed hits, with the
    per-hit score decomposition the SQL computed."""
    hits = [
        SearchHit(
            memory_key=r["memory_key"],
            memory_type=r["memory_type"],
            content_excerpt=r["content_excerpt"],
            distance=float(r["distance"]),
            rank_score=float(r["rank_score"]),
            temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
            temporal_confidence=float(r.get("temporal_confidence") or 0.0),
            entities=[str(x) for x in (r.get("entities") or [])],
            valid_at=_iso_date(str(r.get("valid_at", ""))),
            status=str(r.get("status") or ""),
            version=int(r.get("version") or 0),
            created_at=_iso_date(str(r.get("created_at", ""))),
            source_resource_id=str(r.get("source_resource_id") or ""),
            source_class=str(r.get("source_class") or ""),
            verified=bool(int(r.get("verified") or 0)),
            session_id=str(r.get("session_id") or ""),
            identity=row_identity(r),
            speaker=speaker_from_tags(r.get("tags")),
            about_other="about:other" in [str(t) for t in (r.get("tags") or [])],
            tags=[str(t) for t in (r.get("tags") or []) if t],
            score_components={
                "semantic": max(0.0, 1.0 - float(r["distance"])),
                "rank_score": float(r["rank_score"]),
                # User-derived terms, present only when non-zero.
                **{
                    name: float(r.get(col) or 0.0)
                    for name, col in (
                        ("preference_boost", "preference_boost_sql"),
                        ("reinforcement", "reinforcement_sql"),
                        ("tag_boost", "tag_boost_sql"),
                        ("category_boost", "category_boost_sql"),
                        ("prior_group", "prior_group_sql"),
                    )
                    if float(r.get(col) or 0.0) != 0.0
                },
                # Surfaced so the handler's fusion can attenuate the entity-ANN
                # boost — the same entity signal must not be credited twice.
                **(
                    {"entity_boost_sql": float(r.get("entity_boost_sql") or 0.0)}
                    if float(r.get("entity_boost_sql") or 0.0) > 0.0
                    else {}
                ),
                # Per-hit lexical score, present (even at 0.0) whenever the
                # mode's lexical arm ran, so trace can tell "ran, no overlap"
                # from "never ran".
                **(
                    {"lexical": float(r.get("lexical_sql") or 0.0)}
                    if lexical_score_expr != "0.0"
                    else {}
                ),
                # The excerpt carries one of the query's
                # compounds verbatim — the identifier the reader named.
                **(
                    {"compound_match": 1.0}
                    if compound_match(str(r.get("content_excerpt") or ""), q.lexical_compounds)
                    else {}
                ),
            },
        )
        for r in rows
    ]
    return hits


def _writer_prefix(h: SearchHit) -> tuple[str, str, str]:
    # (agent, user_id, first 60 chars): a hit's leg reads its writer, or reads none
    agent, user_id = h.identity[:2] if h.identity else ("", "")
    return agent, user_id, h.content_excerpt[:60]


async def _merge_leg_hits(
    ch: _CHClient,
    q: SearchQuery,
    hits: list[SearchHit],
    chunk_hits: list[SearchHit],
    lexical_pool: list[SearchHit],
    record_hits: list[SearchHit],
) -> list[SearchHit]:
    """Phase 10 of `vector_search`: fold the chunk, lexical and record legs
    into the fact hits, dedup across planes, and append the unmatched
    candidates when asked."""
    if chunk_hits:
        # Drop a chunk whose first 60 chars match a fact of its own writer — the fact
        # carries the structured signal, the chunk would be redundant.
        fact_prefixes = {_writer_prefix(h) for h in hits if h.source == "fact"}
        chunk_hits = [c for c in chunk_hits if _writer_prefix(c) not in fact_prefixes]
        hits.extend(chunk_hits)

    # Merge the BM25-only union (from the gather), deduped against the rows the ANN and
    # chunk legs found here rather than during fetch.
    existing_rows = {hit_row_key(h) for h in hits}
    if lexical_pool:
        hits.extend(h for h in lexical_pool if hit_row_key(h) not in existing_rows)

    if record_hits:
        hits.extend(record_hits)

    # A row seen by several planes with the same excerpt collapses to its strongest hit;
    # distinct rows of a key stay, even when their excerpts open alike.
    if hits:
        deduped: dict[tuple[RowKey, str], SearchHit] = {}
        for hit in hits:
            dk = (hit_row_key(hit), hit.content_excerpt)
            prior = deduped.get(dk)
            if prior is None or (hit.rank_score, -hit.distance) > (
                prior.rank_score,
                -prior.distance,
            ):
                deduped[dk] = hit
        # A record row is a projection of its parent fact: drop it when a
        # stronger fact row exists for the same key. A record names no memory row,
        # so any writer's fact of the key counts.
        best_fact: dict[str, SearchHit] = {}
        for h in deduped.values():
            if h.source == "fact":
                cur = best_fact.get(h.memory_key)
                if cur is None or (h.rank_score, -h.distance) > (
                    cur.rank_score,
                    -cur.distance,
                ):
                    best_fact[h.memory_key] = h
        hits = [
            h
            for h in deduped.values()
            if not (
                h.source == "record"
                and (fact := best_fact.get(h.memory_key)) is not None
                and (fact.rank_score, -fact.distance) > (h.rank_score, -h.distance)
            )
        ]

    if q.include_top_n_unmatched > 0 and _plane_enabled(q, "unmatched"):
        unmatched_hits = await _fetch_unmatched_candidates(
            ch,
            q,
            hits,
            limit=int(q.include_top_n_unmatched),
        )
        if unmatched_hits:
            hits.extend(unmatched_hits)
    return hits


async def vector_search(
    q: SearchQuery,
    *,
    ch: _CHClient | None = None,
) -> list[SearchHit]:
    """Execute the memory.search SQL and return typed hits.

    Routing targets, dispatched on `q.search_mode`:
      • rrf      — reciprocal-rank fusion of lexical + semantic legs; the
                   parameter-free default. `semantic` / `hybrid` /
                   `bm25_hybrid` select the lexical leg's scoring shape
                   (cosine-only, alpha-blend, IDF-weighted Okapi BM25).
      • graph    — cosine + entity-graph signal (1-hop overlap + triple boost).
      • temporal — date-anchored retrieval over a ±anchor_window band.
      • lexical  — pure token-match (no ANN); composite rank ordering.

    Composition:
      - WHERE: identity / model / dim / not-tombstoned, plus the
        scope=private agent restriction, plus optional
        memory_type / user_id / run_id filters.
      - rank_score: per-mode formula + entity-match bonus.
      - ORDER BY rank_score DESC, distance ASC, LIMIT top_k.
    """
    ch = ch or _default_client()

    q = _apply_plane_mask(q)

    # BM25 precursor: per-token IDF, corpus avgdl
    # and the compounds the corpus knows, onto the frozen query.
    q = await _bm25_precursor(ch, q)

    where, params = _build_where(q)

    t = _fact_boost_terms(q, params)

    _personalize_fact_terms(q, params, t)

    _fact_mode_rank_score(q, where, params, t)

    _fact_rank_tail(q, params, t)

    fact_sql = await _fact_leg_sql(q, ch, where, params, t)
    rows_task = _fact_rows_task(ch, q, params, fact_sql)
    rows, chunk_hits, lexical_pool, record_hits = await _gather_legs(ch, q, params, rows_task)

    hits = _fact_rows_to_hits(rows, q, t.lexical_score_expr)
    hits = await _merge_leg_hits(ch, q, hits, chunk_hits, lexical_pool, record_hits)

    hits = _rerank_fact_pool(hits, q)
    return await _finish_pool(ch, hits, q)


async def vector_search_trace(
    q: SearchQuery,
    *,
    ch: _CHClient | None = None,
) -> list[SearchTraceHit]:
    """Like `vector_search`, but surfaces entity_boost + rank_score as
    separate columns so the dashboard can show each factor's contribution. Shares the
    SQL shape with `vector_search` — keep both in lockstep when ranking changes.
    """
    ch = ch or _default_client()

    where, params = _build_where(q)

    entity_boost_select = "0.0 AS entity_boost"
    if q.entity_filter:
        _raw_ents = [t.lower() for t in q.entity_filter]
        entity_boost_select = (
            "0.5 * length(arrayIntersect(e.entities, {ents:Array(String)})) "
            "/ greatest(toFloat32(length({ents:Array(String)})), 1.0) "
            "AS entity_boost"
        )
        params["ents"] = _raw_ents

    sql = f"""
        SELECT
            e.memory_key       AS memory_key,
            e.memory_type      AS memory_type,
            e.content_excerpt  AS content_excerpt,
            e.status           AS status,
            e.version          AS version,
            cosineDistance(e.embedding, {{q:Array(Float32)}}) AS distance,
            {entity_boost_select},
            (1.0 - distance) + entity_boost AS rank_score
        FROM loom.memory_embeddings AS e FINAL
        WHERE {" AND ".join(where)}
        ORDER BY rank_score DESC, distance ASC, e.memory_key ASC
        LIMIT {{top_k:UInt32}}
        SETTINGS query_plan_direct_read_from_text_index = 0
    """
    rows = await ch.query_all(sql, params=params)
    return [
        SearchTraceHit(
            memory_key=r["memory_key"],
            memory_type=r["memory_type"],
            content_excerpt=r["content_excerpt"],
            distance=float(r["distance"]),
            entity_boost=float(r["entity_boost"]),
            rank_score=float(r["rank_score"]),
        )
        for r in rows
    ]
