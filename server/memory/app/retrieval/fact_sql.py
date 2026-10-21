"""The fact leg's statement: the distance expression and the assembled SELECT."""

from __future__ import annotations

from typing import Any

from server.core.vectors.patch_parts import fact_codes_first
from server.core.vectors.search_settings import vector_search_settings
from server.memory.app.retrieval.fact_rank import _FactRankTerms
from server.memory.app.retrieval.key_axis import resolve_key_axis
from server.memory.domain.search_types import SearchQuery, _CHClient


def _main_leg_threads_sql(settings_obj: Any) -> str:
    """The thread pin on the main fact statement is a setting.
    `max_threads = 4` measured neutral on the plain FINAL scan; under the codes
    plan the same table serves (quantized shortlist, lazy
    join over many parts) the pin cost server time for identical rows. 0 = no pin (the threads follow the marks)."""
    pin = int(settings_obj.memory_search_fact_leg_max_threads or 0)
    return f"max_threads = {pin},\n            " if pin > 0 else ""


async def _fact_distance_expr(q: SearchQuery, ch: _CHClient, settings_obj: Any) -> str:
    """Phase 6's vector fragment: the `distance` expression. Resolves the key axis for this
    read."""
    # Key-embedding axis. Referencing `memory_key_embedding`
    # at all makes ClickHouse read the whole bf16 column for every candidate
    # row — the eager IF cannot skip it, and lazy materialization does not
    # defer a column the ORDER BY's SELECT list computes on. Off, `distance`
    # is the bare value distance and the column is never named in the query.
    key_axis_on = await resolve_key_axis(q, ch=ch, settings_obj=settings_obj)
    if not key_axis_on:
        return "cosineDistance(e.embedding, {q:Array(Float32)})"
    return (
        "least("
        "cosineDistance(e.embedding, {q:Array(Float32)}), "
        "cosineDistance("
        "if(e.memory_key_embedding_dim = length({q:Array(Float32)})"
        " AND length(e.memory_key_embedding) = length({q:Array(Float32)}),"
        " e.memory_key_embedding, e.embedding), {q:Array(Float32)})"
        ")"
    )


async def _fact_leg_sql(
    q: SearchQuery, ch: _CHClient, where: list[str], params: dict[str, Any], t: _FactRankTerms
) -> str:
    """Phase 6 of `vector_search`: the fact leg's statement. Binds
    `internal_limit` into `params`."""
    # Over-fetch 4× top_k (floor 60) so weak semantic candidates can rerank up
    # on lexical / entity signals; truncate to top_k after the SQL returns.
    internal_limit = max(int(q.top_k) * 4, 60)
    params["internal_limit"] = internal_limit

    # The codes-first read applies only when ORDER BY is a BARE
    # cosineDistance(col, ref). The `least(value_dist, key_dist)` composite
    # that `distance` aliases does NOT qualify (full sort). So vector modes
    # ORDER BY a bare value_distance alias and keep `distance` in the SELECT
    # for scoring. The shortlist then sees only the value-embedding axis; the
    # 4× over-fetch partially covers the key-embedding gap. Lexical keeps
    # composite ordering (token WHERE filter).
    if q.search_mode in ("semantic", "hybrid", "bm25_hybrid", "rrf"):
        value_distance_select = "cosineDistance(e.embedding, {q:Array(Float32)}) AS value_distance,"
        order_clause = "ORDER BY value_distance ASC"
    else:  # lexical — token-match WHERE filter
        value_distance_select = ""
        order_clause = "ORDER BY rank_score DESC, distance ASC, e.memory_key ASC"

    # PREWHERE only the identity tuple (stable across versions). PREWHERE runs
    # BEFORE ReplacingMergeTree FINAL collapses parts, so a state-of-the-row
    # predicate (tombstoned_at, status) in PREWHERE would drop the tombstone
    # row and let FINAL return the original as if undeleted. Those filters
    # MUST stay in WHERE (post-merge).
    prewhere_keys = {
        "e.org = {org:String}",
        "e.namespace = {ns:String}",
        "e.scope = {scope:String}",
    }
    prewhere_clauses = [w for w in where if w in prewhere_keys]
    rest_clauses = [w for w in where if w not in prewhere_keys]

    from server.settings import settings as _vs_settings

    # This leg's identity PREWHERE prunes granules
    # before FINAL merges them, so FINAL is the cheaper plan here (42 vs
    # 103 ms on a 148-row namespace, same bytes, identical rows).
    from_sql = "FROM loom.memory_embeddings AS e FINAL"
    prewhere_sql = " PREWHERE " + " AND ".join(prewhere_clauses) if prewhere_clauses else ""
    where_sql = " WHERE " + " AND ".join(rest_clauses) if rest_clauses else ""

    # use_query_cache is deliberately NOT set — it breaks write-then-read.
    distance_expr = await _fact_distance_expr(q, ch, _vs_settings)
    select_from = f"""
        SELECT
            e.memory_key       AS memory_key,
            e.memory_type      AS memory_type,
            e.content_excerpt  AS content_excerpt,
            e.status           AS status,
            e.version          AS version,
            -- Multi-vector axis: min(value_dist, key_dist). CH's IF is eager,
            -- so we can't gate cosineDistance on key-dim; the inner IF falls
            -- back to the value embedding on dim mismatch, making `least` a
            -- no-op for rows without a key embedding.
            {distance_expr} AS distance,
            {value_distance_select}
            ({t.entity_boost_expr}) AS entity_boost_sql,
            ({t.lexical_score_expr}) AS lexical_sql,
            -- Decomposition of the user-derived terms, so a
            -- caller can see what personalization added to each hit.
            ({t.preference_boost_expr}) AS preference_boost_sql,
            ({t.reinforcement_boost_expr}) AS reinforcement_sql,
            ({t.tag_boost_expr}) AS tag_boost_sql,
            ({t.category_boost_expr}) AS category_boost_sql,
            ({t.prior_group_select}) AS prior_group_sql,
            {t.rank_score_expr} AS rank_score,
            toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
            e.temporal_confidence AS temporal_confidence,
            toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
            toString(coalesce(e.created_at, toDateTime(0))) AS created_at,
            e.entities AS entities,
            ifNull(e.source_resource_id, '') AS source_resource_id,
            e.source_class AS source_class,
            e.verified AS verified,
            e.session_id AS session_id,
            e.tags AS tags,
            -- The folded reinforcement lives in a side table
            -- keyed on (agent, user_id, memory_key); the fact leg reads it
            -- after this fetch for its candidates and re-scores from the
            -- write-time base carried here.
            e.agent AS agent,
            e.user_id AS user_id,
            e.reinforcement_count AS reinforcement_base,
            e.last_reinforced_at AS last_reinforced_base
        {from_sql}
        {prewhere_sql}
        {where_sql}
    """
    # The codes-first read on the main leg. The codes pass is
    # lazy materialization and ClickHouse skips the lazy step while the table
    # carries a patch part, so with patch parts present the
    # flag reads MORE than the plain scan (304 vs 177 ms on a large tenant).
    # `fact_codes_first` says yes only under `on`, or under `auto` when the
    # 60 s probe of system.parts read zero patch parts; the shortlist is then
    # LIMIT x `memory_search_fact_codes_fetch_multiplier` (3.0; x1 changed the
    # top 10 on a quarter of queries).
    # Off, the statement carries no vector settings: an exact scan. No table
    # has a vector index; the identity PREWHERE keeps the read to the tenant's
    # granules, and ClickHouse sorts their distances exactly.
    codes_first = await fact_codes_first(ch, settings_obj=_vs_settings)
    codes_sql = vector_search_settings(
        fetch_multiplier=float(_vs_settings.memory_search_fact_codes_fetch_multiplier),
        quantized=codes_first,
    )
    settings_head = f"{codes_sql}," if codes_sql else "SETTINGS"
    main_threads_sql = _main_leg_threads_sql(_vs_settings)
    sql = f"""{select_from}
        {order_clause}
        LIMIT {{internal_limit:UInt32}}
        {settings_head}
            {main_threads_sql}-- A text index on content_excerpt_lc, if one is declared, fires on
            -- hasAnyTokens with no per-query setting. Its DIRECT-READ
            -- shortcut (the __text_index_*_hasAnyTokens virtual columns) hands
            -- back an empty block without the PREWHERE columns under FINAL for
            -- some token sets — code 10, deterministic on 26.6. The shortcut stays off.
            query_plan_direct_read_from_text_index = 0,
            use_statistics_cache = 1,
            allow_statistics_optimize = 1,
            query_plan_join_swap_table = 'auto',
            optimize_read_in_order = 1
    """
    return sql
