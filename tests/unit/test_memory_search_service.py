"""Unit tests for server/memory/app/retrieval/ — SQL shape, mocked CH."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from server.memory.app.retrieval import (
    SearchHit,
    SearchQuery,
    SearchTraceHit,
    fetch_triple_matched_memories,
    vector_search,
    vector_search_trace,
)
from server.settings import settings


@pytest.fixture(autouse=True)
def _key_axis_pinned(monkeypatch: pytest.MonkeyPatch):
    """The deployment default is "auto", which probes
    the namespace's key reuse through the client before the fact leg runs;
    these shape tests stub one statement, so they pin the axis off unless a
    test sets it."""
    from server.settings import settings as _pinned

    monkeypatch.setattr(_pinned, "memory_search_key_axis", "off")
    yield


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []


def _hit() -> dict[str, Any]:
    return {
        "memory_key": "deploy_window",
        "memory_type": "preference",
        "content_excerpt": "Deploy Tue/Thu, never Friday",
        "distance": 0.21,
        "rank_score": 0.79,
    }


def _q(**overrides) -> SearchQuery:
    base = {
        "org": "clickhouse",
        "namespace": "ns",
        "scope": "private",
        "agent": "bot",
        "embedding_model": "text-embedding-3-small",
        "query_vec": [0.1] * 1536,
        "top_k": 10,
    }
    base.update(overrides)
    return SearchQuery(**base)


@pytest.mark.asyncio
async def test_private_scope_filters_by_agent() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_hit()])
    out = await vector_search(_q(scope="private", agent="claude-desktop"), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "e.agent = {agent:String}" in sql
    assert params["agent"] == "claude-desktop"
    assert len(out) == 1 and isinstance(out[0], SearchHit)


@pytest.mark.asyncio
async def test_shared_scope_omits_agent_filter() -> None:
    """Shared memory is namespace-wide; scope filter alone, no agent clause."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(scope="shared"), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "e.agent = {agent:String}" not in sql
    assert "agent" not in params


@pytest.mark.asyncio
async def test_agent_dash_sentinel_behaves_like_wildcard() -> None:
    """agent='-' (the identity default sentinel = 'no specific agent') must omit
    the agent filter, exactly like agent='*' — never filter on the literal '-',
    which no stored row carries (would return zero real-agent rows). Separate
    stubs so each invocation's main query is calls[0] (vector_search issues
    follow-on chunk queries too)."""
    ch_dash = StubCH()
    ch_dash.query_all_returns.append([])
    await vector_search(_q(scope="private", agent="-"), ch=ch_dash)
    sql_dash, params_dash = ch_dash.query_all_calls[0]
    assert "e.agent = {agent:String}" not in sql_dash
    assert "agent" not in params_dash

    # The main query is identical to the '*' (cross-agent) path, byte-for-byte.
    ch_star = StubCH()
    ch_star.query_all_returns.append([])
    await vector_search(_q(scope="private", agent="*"), ch=ch_star)
    sql_star, params_star = ch_star.query_all_calls[0]
    assert sql_dash == sql_star
    assert params_dash == params_star


@pytest.mark.asyncio
async def test_optional_filters_thread_through() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(
        _q(
            memory_type="procedural",
            user_id="alice@example.com",
            run_id="r-7",
        ),
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "e.memory_type = {type:String}" in sql
    assert "e.user_id = {user_id:String}" in sql
    assert "e.run_id = {run_id:String}" in sql
    assert params["type"] == "procedural"
    assert params["user_id"] == "alice@example.com"
    assert params["run_id"] == "r-7"


@pytest.mark.asyncio
async def test_triple_match_filters_to_current_valid_triples() -> None:
    ch = StubCH()
    ch.query_all_returns.append([{"memory_key": "current_employer"}])

    out = await fetch_triple_matched_memories(
        ch,
        org="clickhouse",
        namespace="ns",
        query_triples=[("user", "works_at", "clickhouse")],
        expand_synonyms=False,
    )

    sql, params = ch.query_all_calls[0]
    assert out == ["current_employer"]
    assert "INNER JOIN loom.entity_triples AS t" in sql
    assert "t.valid_to IS NULL OR t.valid_to >= now()" in sql
    assert "tm.subject = {sub0:String}" in sql
    assert params["sub0"] == "user"


@pytest.mark.asyncio
async def test_triple_match_as_of_uses_interval_containment() -> None:
    from datetime import datetime

    ch = StubCH()
    as_of = datetime(2024, 6, 1)
    await fetch_triple_matched_memories(
        ch,
        org="clickhouse",
        namespace="ns",
        query_triples=[("user", "works_at", "acme")],
        expand_synonyms=False,
        as_of=as_of,
    )

    sql, params = ch.query_all_calls[0]
    assert "t.valid_from IS NULL OR t.valid_from <= {as_of:DateTime}" in sql
    assert "t.valid_to IS NULL OR t.valid_to >= {as_of:DateTime}" in sql
    assert params["as_of"] == as_of


@pytest.mark.asyncio
async def test_entity_filter_adds_additive_boost_not_gating() -> None:
    """Entity match is additive: boost in the SELECT/rank_score, not the WHERE."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(entity_filter=["Alice", "Bob"]), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "arrayIntersect(e.entities, {ents:Array(String)})" in sql
    where_clause = sql.split("WHERE")[1].split("ORDER")[0]
    assert "hasAny(e.entities" not in where_clause
    assert params["ents"] == ["alice", "bob"]  # canonical lowercase


@pytest.mark.asyncio
async def test_no_entity_filter_emits_zero_boost_expr() -> None:
    """Without entity_filter the SQL must not reference {ents:...} (missing-param)."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(entity_filter=[]), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "{ents:Array(String)}" not in sql
    assert "ents" not in params


@pytest.mark.asyncio
async def test_chunk_entity_overlap_uses_noncorrelated_join() -> None:
    """entity_overlap_count must be a non-correlated LEFT JOIN, not a correlated
    scalar subquery (Code 48 NOT_IMPLEMENTED on CH kills the chunk plane)."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "sess1", "chunk_index": 3, "distance": 0.2}])
    ch.query_all_returns.append([])
    q = _q(entity_filter=["Spotify"], query_text="how many playlists")
    parent_params = {"q": q.query_vec}
    await _search_memory_chunks(ch, q, parent_params)
    sql, params = ch.query_all_calls[1]  # [0] is the stage-1 candidate read

    assert "LEFT JOIN" in sql
    assert "ON eo.memory_key = c.parent_memory_key" in sql
    assert "memory_key = parent_memory_key" not in sql.replace(" ", " ")
    assert "= c.parent_memory_key" in sql  # only as the JOIN key, post-grouping
    overlap_block = sql.split("LEFT JOIN", 1)[1].split(") AS eo", 1)[0]
    assert "LIMIT" not in overlap_block
    assert params["ent_filter"] == ["spotify"]


@pytest.mark.asyncio
async def test_chunk_no_entity_filter_emits_no_entity_join() -> None:
    """No entity_filter: omit the entity-overlap JOIN and {ent_filter:...} param;
    the unconditional parent-date JOIN stays."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "sess1", "chunk_index": 3, "distance": 0.2}])
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="how many playlists")
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    sql, params = ch.query_all_calls[1]  # [0] is the stage-1 candidate read
    assert ") AS eo" not in sql
    assert "{ent_filter:Array(String)}" not in sql
    assert "ent_filter" not in params
    assert ") AS pd ON pd.pd_memory_key = c.parent_memory_key" in sql
    assert "coalesce(ss.session_date, pd.temporal_anchor, toDateTime(0))" in sql


@pytest.mark.asyncio
async def test_chunk_plane_logs_on_query_failure(caplog) -> None:
    """A chunk-query error must be logged, not silently swallowed; fail-open."""
    import logging

    from server.memory.app.retrieval.chunks import _search_memory_chunks

    class BoomCH:
        async def query_all(self, sql, params=None):
            raise RuntimeError("Code 48: NOT_IMPLEMENTED")

    q = _q(entity_filter=["Spotify"], query_text="how many playlists")
    with caplog.at_level(logging.ERROR, logger="server.memory.app.retrieval"):
        out = await _search_memory_chunks(BoomCH(), q, {"q": q.query_vec})
    assert out == []  # fail-open
    assert any("chunk plane query failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_chunk_hit_is_dated_by_its_conversation() -> None:
    """A chunk hit carries the conversation date (the session
    digest's valid_at) — the query coalesces it ahead of the parent fact's
    event anchor, and the parent join stays as the fallback."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "sess1", "chunk_index": 3, "distance": 0.18}])
    ch.query_all_returns.append(
        [
            {
                "memory_key": "chunk:sess1:3",
                "memory_type": "episodic",
                "content_excerpt": "User got new binoculars yesterday.",
                "content_full": "User got new binoculars yesterday.",
                "session_id": "sess1",
                "chunk_index": 3,
                "parent_memory_key": "user_binoculars",
                "distance": 0.18,
                "entity_overlap_count": 0,
                "temporal_anchor": "2023-05-20 00:00:00",
                "valid_at": "2023-05-20 00:00:00",
            }
        ]
    )
    out = await _search_memory_chunks(
        ch,
        _q(entity_filter=[], query_text="binoculars", top_k=5),
        {"q": [0.1] * 1536},
    )
    assert len(out) == 1
    assert out[0].source == "chunk"
    assert out[0].temporal_anchor == "2023-05-20 00:00:00"
    assert out[0].valid_at == "2023-05-20 00:00:00"
    sql, _params = ch.query_all_calls[1]
    assert "loom.session_summaries" in sql
    assert "coalesce(ss.session_date, pd.temporal_anchor" in sql
    assert "coalesce(ss.session_date, pd.valid_at" in sql
    # The parent join is still there as the fallback for digest-less sessions.
    assert "pd.pd_memory_key = c.parent_memory_key" in sql


def test_chunk_date_sql_switch() -> None:
    """`memory_search_chunk_date_source` picks the dating: `session` joins the
    digest and coalesces it first; `parent` is the event anchor only."""
    from server.memory.app.retrieval.chunks import _chunk_date_sql

    sel, join = _chunk_date_sql("session")
    assert "ss.session_date" in sel and "loom.session_summaries" in join
    assert "pd.pd_memory_key = c.parent_memory_key" in join
    sel_p, join_p = _chunk_date_sql("parent")
    assert "ss.session_date" not in sel_p and "session_summaries" not in join_p
    assert "pd.temporal_anchor" in sel_p
    # The clauses bind only parameters every chunk query already carries.
    for clause in (join, join_p):
        assert "{org:String}" in clause and "{ns:String}" in clause


@pytest.mark.asyncio
async def test_chunk_neighbors_dated_like_anchors() -> None:
    """The neighbour-expansion query dates ±1 chunks the same way."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "sess1", "chunk_index": 3, "distance": 0.18}])
    ch.query_all_returns.append(
        [
            {
                "memory_key": "chunk:sess1:3",
                "memory_type": "episodic",
                "content_excerpt": "User got new binoculars yesterday.",
                "content_full": "User got new binoculars yesterday.",
                "session_id": "sess1",
                "chunk_index": 3,
                "parent_memory_key": "user_binoculars",
                "distance": 0.18,
                "entity_overlap_count": 0,
                "temporal_anchor": "2023-05-20 00:00:00",
                "valid_at": "2023-05-20 00:00:00",
            }
        ]
    )
    await _search_memory_chunks(
        ch,
        _q(entity_filter=[], query_text="binoculars", top_k=5),
        {"q": [0.1] * 1536},
    )
    # The ±1 windows ride in the stage-2 statement, dated by
    # the same joins as their anchors (one set of joins outside the union).
    assert len(ch.query_all_calls) == 2
    stage2_sql, _ = ch.query_all_calls[1]
    assert "1 AS is_neighbor" in stage2_sql
    assert "loom.session_summaries" in stage2_sql
    assert stage2_sql.count("coalesce(ss.session_date, pd.temporal_anchor") == 1


@pytest.mark.asyncio
async def test_chunk_orphan_stays_undated() -> None:
    """An orphan chunk (parent JOIN missed → epoch sentinel) stays undated (empty
    string), not falsely stamped 1970-01-01."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "sess9", "chunk_index": 0, "distance": 0.22}])
    ch.query_all_returns.append(
        [
            {
                "memory_key": "chunk:sess9:0",
                "memory_type": "episodic",
                "content_excerpt": "Orphan chunk with no extracted parent.",
                "content_full": "Orphan chunk with no extracted parent.",
                "session_id": "sess9",
                "chunk_index": 0,
                "parent_memory_key": "",
                "distance": 0.22,
                "entity_overlap_count": 0,
                "temporal_anchor": "1970-01-01 00:00:00",
                "valid_at": "1970-01-01 00:00:00",
            }
        ]
    )
    out = await _search_memory_chunks(
        ch,
        _q(entity_filter=[], query_text="orphan", top_k=5),
        {"q": [0.1] * 1536},
    )
    assert len(out) == 1
    assert out[0].temporal_anchor == ""
    assert out[0].valid_at == ""


@pytest.mark.asyncio
async def test_semantic_mode_orders_by_bare_cosine() -> None:
    """Order by the bare cosineDistance alias (the codes-first read needs that shape);
    the least() composite stays in the SELECT for scoring."""
    ch = StubCH()
    ch.query_all_returns.append([])
    # key_axis=True: the least() composite is the key-axis shape (the shape is
    # pinned per request).
    await vector_search(_q(search_mode="semantic", key_axis=True), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "cosineDistance(e.embedding, {q:Array(Float32)}) AS value_distance" in sql
    assert "ORDER BY value_distance ASC" in sql
    assert "ORDER BY distance ASC" not in sql
    assert "least(" in sql
    assert "{internal_limit:UInt32}" in sql


@pytest.mark.asyncio
async def test_hybrid_mode_orders_by_bare_cosine() -> None:
    """Hybrid also orders by the bare cosineDistance alias."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(search_mode="hybrid"), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "ORDER BY value_distance ASC" in sql


@pytest.mark.asyncio
async def test_lexical_mode_orders_by_rank_score() -> None:
    """Lexical keeps composite rank_score order — token-based WHERE, not ANN."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(search_mode="lexical"), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "ORDER BY rank_score DESC, distance ASC" in sql


@pytest.mark.asyncio
async def test_vector_search_trace_exposes_entity_boost_and_rank_score() -> None:
    """Trace variant returns entity_boost + rank_score as separate SearchTraceHit
    fields for per-component contributions."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                **_hit(),
                "entity_boost": 0.5,
                "rank_score": 1.62,
            }
        ]
    )
    out = await vector_search_trace(_q(entity_filter=["Alice"]), ch=ch)
    assert len(out) == 1
    h = out[0]
    assert isinstance(h, SearchTraceHit)
    assert h.entity_boost == 0.5
    assert h.rank_score == 1.62


@pytest.mark.asyncio
async def test_include_top_n_unmatched_fetches_recency_pool() -> None:
    """include_top_n_unmatched adds a bounded fallback pool after primary miss."""
    ch = StubCH()
    ch.query_all_returns.extend(
        [
            [],  # primary fact search
            [],  # chunk search
            [
                {
                    "memory_key": "older_but_useful",
                    "memory_type": "semantic",
                    "content_excerpt": "Older useful memory",
                    "distance": 0.91,
                    "temporal_anchor": "1970-01-01 00:00:00",
                    "valid_at": "1970-01-01 00:00:00",
                    "source_resource_id": "",
                    "confidence": 0.8,
                    "reinforcement_count": 3,
                }
            ],
        ]
    )

    out = await vector_search(
        _q(query_text="", include_top_n_unmatched=1, search_mode="semantic"),
        ch=ch,
    )

    assert [h.memory_key for h in out] == ["older_but_useful"]
    assert out[0].score_components["unmatched_confidence"] > 0
    unmatched_sql, unmatched_params = ch.query_all_calls[-1]
    assert "ORDER BY reinforcement_count DESC, confidence DESC, freshness DESC" in unmatched_sql
    assert unmatched_params["unmatched_limit"] == 1


@pytest.mark.asyncio
async def test_execution_planes_fact_only_skips_chunk_lexical_and_unmatched() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])

    out = await vector_search(
        _q(
            query_text="deploy window",
            search_mode="rrf",
            include_top_n_unmatched=25,
            execution_planes=["fact"],
        ),
        ch=ch,
    )

    assert out == []
    assert len(ch.query_all_calls) == 1
    sql, _ = ch.query_all_calls[0]
    assert "FROM loom.memory_embeddings" in sql
    assert "ORDER BY value_distance ASC" in sql


@pytest.mark.asyncio
async def test_record_plane_adds_schema_record_hits() -> None:
    ch = StubCH()
    ch.query_all_returns.extend(
        [
            [],  # primary fact search
            [
                {
                    "record_id": "11111111-2222-3333-4444-555555555555",
                    "parent_memory_key": "profile_theme",
                    "schema_name": "preference",
                    "record_json": '{"theme": "dark"}',
                    "field_text": "theme: dark",
                    "lexical_score": 1.0,
                    "rank_score": 0.8,
                    "valid_at": "1970-01-01 00:00:00",
                    "temporal_anchor": "1970-01-01 00:00:00",
                }
            ],
        ]
    )

    out = await vector_search(
        _q(
            query_text="what theme do I use",
            execution_planes=["fact", "record"],
        ),
        ch=ch,
    )

    assert [h.source for h in out] == ["record"]
    assert out[0].memory_key == "profile_theme"
    assert out[0].memory_type == "record"
    assert out[0].source_resource_id == "11111111-2222-3333-4444-555555555555"
    assert "theme=dark" in out[0].content_excerpt
    record_sql, record_params = ch.query_all_calls[-1]
    assert "FROM loom.memory_records" in record_sql
    assert "LEFT JOIN loom.memory_record_fields" in record_sql
    assert "theme" in record_params["tokens"]
    assert record_params["record_agents"] == ["bot", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound,terms",
    [
        ({}, []),
        ({"session_id": "s1"}, ["r.session_id = {session_id:String}"]),
        ({"session_ids": ["s1", "s2"]}, ["r.session_id IN {session_ids:Array(String)}"]),
    ],
)
async def test_record_plane_keeps_to_the_read_session_bound(
    bound: dict[str, Any], terms: list[str]
) -> None:
    from server.memory.app.retrieval.side_legs import _search_memory_records

    ch = StubCH()
    await _search_memory_records(ch, _q(query_text="what theme do I use", **bound))
    (sql, params) = ch.query_all_calls[0]
    assert [t for t in ("r.session_id = {", "r.session_id IN {") if t in sql] == [
        t.split("{")[0] + "{" for t in terms
    ]
    for key in ("session_id", "session_ids"):
        assert (key in params) == (key in bound)
        if key in bound:
            assert params[key] == bound[key]


@pytest.mark.asyncio
async def test_record_plane_respects_unrepresentable_hard_filters() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])

    await vector_search(
        _q(
            query_text="theme",
            execution_planes=["fact", "record"],
            tags=["profile"],
        ),
        ch=ch,
    )

    assert len(ch.query_all_calls) == 1
    sql, _ = ch.query_all_calls[0]
    assert "FROM loom.memory_embeddings" in sql
    assert "FROM loom.memory_records" not in sql


@pytest.mark.asyncio
async def test_bm25_hybrid_uses_aligned_bm25_tokens() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])

    await vector_search(
        _q(
            query_text="deploy deploys",
            search_mode="bm25_hybrid",
            bm25_tokens=["deploy", "deploys"],
            bm25_idfs=[1.0, 2.0],
            bm25_avgdl=40.0,
            execution_planes=["fact", "lexical"],
        ),
        ch=ch,
    )

    _, params = ch.query_all_calls[0]
    assert params["query_tokens"] == ["deploy", "deploys"]
    assert params["bm25_idfs"] == [1.0, 2.0]


@pytest.mark.asyncio
async def test_rrf_mode_computes_lexical_leg_in_sql() -> None:
    """rrf includes the token-overlap lexical expression in SQL rank_score (the
    Python RRF ranks one arm by it) but adds no token WHERE clause."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(
        _q(
            query_text="deploy window",
            search_mode="rrf",
            execution_planes=["fact"],  # planner downgrades rrf without "lexical"
        ),
        ch=ch,
    )
    sql, _ = ch.query_all_calls[0]
    assert "{score_tokens:Array(String)}" not in sql  # downgraded to semantic

    ch2 = StubCH()
    ch2.query_all_returns.append([])
    await vector_search(
        _q(
            query_text="deploy window",
            search_mode="rrf",
            execution_planes=["fact", "lexical"],
        ),
        ch=ch2,
    )
    sql2, params2 = ch2.query_all_calls[0]
    assert "{score_tokens:Array(String)}" in sql2
    assert "deploy" in params2["score_tokens"]
    # Lexical leg is SELECT-side score only; no candidate-pool token filter.
    where_clause = sql2.split("WHERE", 1)[1].split("ORDER", 1)[0]
    assert "hasAnyTokens" not in where_clause


@pytest.mark.asyncio
async def test_rrf_by_lex_differs_from_by_sem_under_token_overlap() -> None:
    """With the lexical leg live, the RRF by_lex arm ranks by genuine token overlap,
    fusing to a different order than semantic-only doubling."""
    rows = [
        {**_hit(), "memory_key": "a_sem", "distance": 0.2, "rank_score": 0.0},
        {**_hit(), "memory_key": "b_lex", "distance": 0.3, "rank_score": 1.0},
        {**_hit(), "memory_key": "c_mid", "distance": 0.4, "rank_score": 0.5},
    ]
    ch = StubCH()
    ch.query_all_returns.append(list(rows))
    out = await vector_search(
        _q(
            query_text="deploy window",
            search_mode="rrf",
            execution_planes=["fact", "lexical"],
        ),
        ch=ch,
    )
    # by_sem=[a,b,c]; by_lex=[b,c,a]; RRF fuses to [b,a,c].
    assert [h.memory_key for h in out] == ["b_lex", "a_sem", "c_mid"]
    assert all("rrf" in h.score_components for h in out)

    # Control: dead lexical arm (equal rank_score) degenerates to semantic order.
    ch2 = StubCH()
    ch2.query_all_returns.append([{**r, "rank_score": 0.0} for r in rows])
    out2 = await vector_search(
        _q(
            query_text="deploy window",
            search_mode="rrf",
            execution_planes=["fact", "lexical"],
        ),
        ch=ch2,
    )
    assert [h.memory_key for h in out2] == ["a_sem", "b_lex", "c_mid"]


@pytest.mark.asyncio
async def test_dedup_keeps_same_key_different_content() -> None:
    """Two distinct facts under the same memory_key both survive dedup; exact
    same-content duplicates still collapse."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                **_hit(),
                "memory_key": "user_event",
                "content_excerpt": "took a week-long break in January",
                "distance": 0.30,
                "rank_score": 0.70,
            },
            {
                **_hit(),
                "memory_key": "user_event",
                "content_excerpt": "took a month-long break in February",
                "distance": 0.35,
                "rank_score": 0.65,
            },
            # Exact duplicate of the first: collapses to one.
            {
                **_hit(),
                "memory_key": "user_event",
                "content_excerpt": "took a week-long break in January",
                "distance": 0.32,
                "rank_score": 0.60,
            },
        ]
    )
    out = await vector_search(
        _q(search_mode="semantic", execution_planes=["fact"]),
        ch=ch,
    )
    excerpts = sorted(h.content_excerpt for h in out)
    assert excerpts == [
        "took a month-long break in February",
        "took a week-long break in January",
    ]
    # Duplicate kept the stronger candidate.
    jan = next(h for h in out if "January" in h.content_excerpt)
    assert jan.rank_score == 0.70


@pytest.mark.asyncio
async def test_record_projection_still_collapses_against_stronger_fact() -> None:
    """A record row is a projection of its parent fact: when a stronger fact row
    exists for the same parent key, the record row is dropped."""
    ch = StubCH()
    ch.query_all_returns.extend(
        [
            [
                {
                    **_hit(),
                    "memory_key": "profile_theme",
                    "content_excerpt": "User prefers the dark theme",
                    "distance": 0.2,
                    "rank_score": 0.9,
                }
            ],
            [
                {
                    "record_id": "11111111-2222-3333-4444-555555555555",
                    "parent_memory_key": "profile_theme",
                    "schema_name": "preference",
                    "record_json": '{"theme": "dark"}',
                    "field_text": "theme: dark",
                    "lexical_score": 0.5,
                    "rank_score": 0.4,
                    "valid_at": "1970-01-01 00:00:00",
                    "temporal_anchor": "1970-01-01 00:00:00",
                }
            ],
        ]
    )
    out = await vector_search(
        _q(
            query_text="what theme do I use",
            search_mode="semantic",
            execution_planes=["fact", "record"],
        ),
        ch=ch,
    )
    assert [h.source for h in out] == ["fact"]
    assert out[0].memory_key == "profile_theme"


@pytest.mark.asyncio
async def test_entity_boost_sql_surfaces_in_score_components() -> None:
    """The SQL-side exact-intersection entity boost is surfaced per row so fusion
    can attenuate the entity-ANN boost instead of double-crediting."""
    ch = StubCH()
    ch.query_all_returns.append(
        [{**_hit(), "entity_boost_sql": 0.25}, {**_hit(), "memory_key": "k2"}]
    )
    out = await vector_search(
        _q(entity_filter=["Alice"], execution_planes=["fact"]),
        ch=ch,
    )
    by_key = {h.memory_key: h for h in out}
    assert by_key["deploy_window"].score_components["entity_boost_sql"] == 0.25
    assert "entity_boost_sql" not in by_key["k2"].score_components


@pytest.mark.asyncio
async def test_lexical_fallback_applies_caller_filters() -> None:
    """The BM25 lexical-recall pool honors the same caller filters as the vector
    pool (via shared _build_where), adding only the lexical token clause on top."""
    from server.memory.app.retrieval.side_legs import _lexical_recall_fallback

    ch = StubCH()
    ch.query_all_returns = [[]]
    q = _q(tags=["polarity:negative"], query_text="flaky mergetree test")
    await _lexical_recall_fallback(ch, q, limit=20)

    assert ch.query_all_calls, "expected a lexical query"
    sql, params = ch.query_all_calls[0]
    assert "hasAny(e.tags" in sql  # tag filter
    assert params.get("tags") == ["polarity:negative"]
    assert "e.embedding_model = {model:String}" in sql
    assert "NOT IN {non_searchable" in sql  # memory_type=None → exclusion
    assert "e.expires_at" in sql  # TTL applied
    assert "hasAnyTokens" in sql  # lexical token clause


@pytest.mark.asyncio
async def test_lexical_fallback_matches_the_three_text_columns() -> None:
    import server.memory.app.retrieval.side_legs as ms

    class _StubCH:
        def __init__(self) -> None:
            self.query_all_calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql, params=None):
            self.query_all_calls.append((sql, params or {}))
            return []

    ch = _StubCH()
    q = _q(query_text="flaky mergetree test")
    await ms._lexical_recall_fallback(ch, q, limit=20)
    sql, params = ch.query_all_calls[0]
    assert "hasAnyTokens(e.content_excerpt_lc" in sql
    assert "hasAnyTokens(e.content_excerpt_stem" in sql
    assert "hasAnyTokens(e.content_excerpt_lemma" in sql
    assert params["stem_tokens"] and params["lemma_tokens"]


# --- Bare (minimal-prior) ranking -------------------------------------------
# Semantic mode ranks bare: the rank_score collapses to (nearly) pure cosine
# + correctness-only sinks. These assert on the assembled
# rank_score sub-expression, isolated from the other SELECT columns (which keep
# entity_boost_sql / lexical_sql as separate score components regardless).


def _rank_score_expr(sql: str) -> str:
    """Isolate the `<expr> AS rank_score` sub-expression from the SELECT.

    entity_boost_sql / lexical_sql sit just before it and carry the entity /
    lexical fragments as their OWN columns, so slice between those delimiters
    to test the rank_score expression alone.
    """
    return sql.split("AS lexical_sql,", 1)[1].split("AS rank_score,", 1)[0]


# Boost fragments that must vanish from rank_score in bare mode. Keyed by the
# query field that makes each one non-trivial.
_BOOST_FRAGMENTS = {
    "preference (pref_fallback)": "has(e.tags, 'preference')",
    "entity_boost": "arrayIntersect(e.entities",
    "recency_boost": "0.25 /",
    "confidence prior": "e.confidence",
    "reinforcement_boost": "reinf_cap",
}


def _boosted_semantic_q() -> SearchQuery:
    """A semantic query with every discretionary boost turned on, plus both
    correctness sinks (demote_tags + conflicted_penalty)."""
    return _q(
        search_mode="semantic",
        query_text="deploy window",
        entity_filter=["Alice"],
        recency_intent=True,
        reinforcement_weight=0.2,
        conflicted_penalty=0.1,
        demote_tags=["noise"],
    )


@pytest.mark.asyncio
async def test_bare_ranking_semantic_collapses_to_cosine_plus_correctness(
    monkeypatch,
) -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_boosted_semantic_q(), ch=ch)
    sql, params = ch.query_all_calls[0]
    expr = _rank_score_expr(sql)

    # Pure cosine core survives.
    assert "1.0 - distance" in expr
    # Every discretionary / relevance-inflating boost is gone.
    for name, frag in _BOOST_FRAGMENTS.items():
        assert frag not in expr, f"bare rank_score still carries {name}: {frag!r}"
    # No capped prior group either.
    assert "least(" not in expr
    assert "prior_cap" not in params
    assert "confidence_weight" not in params
    # Correctness-only sinks are retained.
    assert "-1000.0" in expr  # hard demote_tags sink
    assert "e.status = 'conflicted'" in expr  # conflicted demotion
    assert params.get("demote_tags") == ["noise"]
    assert params.get("conflicted_penalty") == 0.1


@pytest.mark.asyncio
async def test_bare_ranking_is_noop_for_non_semantic_modes(monkeypatch) -> None:
    """Bare ranking is scoped to semantic mode; rrf/hybrid keep their boosts."""
    for mode in ("rrf", "hybrid"):
        ch = StubCH()
        ch.query_all_returns.append([])
        q = _q(
            search_mode=mode,
            query_text="deploy window",
            entity_filter=["Alice"],
            recency_intent=True,
        )
        await vector_search(q, ch=ch)
        sql, _ = ch.query_all_calls[0]
        expr = _rank_score_expr(sql)
        assert "arrayIntersect(e.entities" in expr, f"{mode} lost entity_boost"
        assert "has(e.tags, 'preference')" in expr, f"{mode} lost preference"
        assert "0.25 /" in expr, f"{mode} lost recency_boost"


@pytest.mark.asyncio
async def test_vector_leg_retries_the_fresh_part_empty_block_error_once(monkeypatch) -> None:
    """CH 26.7.1 race: for a few seconds after an insert the vector leg can
    fail with code 10 "Not found column org: in block … (size = 0)"; the same
    statement re-run immediately succeeds. Retry it, briefly, and count it."""

    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)
    ch = StubCH()
    calls = {"n": 0}
    real_query_all = ch.query_all

    async def flaky(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 1 and "{internal_limit:UInt32}" in sql:
            raise RuntimeError(
                "Code: 10. DB::Exception: Not found column org: in block memory_key String String(size = 0)"
            )
        return await real_query_all(sql, params)

    ch.query_all = flaky  # type: ignore[method-assign]
    ch.query_all_returns.append([_hit()])
    # Fact plane only: the chunk leg's own two reads
    # would otherwise race this stub for the queued rows.
    out = await vector_search(_q(scope="private", agent="a", execution_planes=["fact"]), ch=ch)
    assert len(out) == 1 and calls["n"] >= 2, "first attempt failed, retry served the rows"


@pytest.mark.asyncio
async def test_vector_leg_does_not_retry_other_errors_or_forever(monkeypatch) -> None:

    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)
    ch = StubCH()

    async def other(sql, params=None):
        raise RuntimeError("Code: 60. DB::Exception: Unknown table")

    ch.query_all = other  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Code: 60"):
        await vector_search(_q(scope="private", agent="a", execution_planes=["fact"]), ch=ch)

    ch2 = StubCH()
    n = {"c": 0}

    async def always(sql, params=None):
        if "{internal_limit:UInt32}" in sql:  # the vector leg only
            n["c"] += 1
        raise RuntimeError("Not found column org: in block memory_key String String(size = 0)")

    ch2.query_all = always  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Not found column"):
        await vector_search(_q(scope="private", agent="a", execution_planes=["fact"]), ch=ch2)
    assert n["c"] == 1 + settings.memory_retrieval_empty_block_retries


@pytest.mark.asyncio
async def test_sensitivity_penalty_demotes_labelled_rows_in_bare_and_boosted_modes(
    monkeypatch,
) -> None:
    """Rows tagged sensitivity:medium|high sink by the penalty
    on both ranking paths; 0.0 leaves the SQL untouched."""
    for mode in ("semantic", "hybrid"):
        ch = StubCH()
        ch.query_all_returns.append([])
        q = dataclasses.replace(_boosted_semantic_q(), sensitivity_penalty=0.1, search_mode=mode)
        await vector_search(q, ch=ch)
        vec = [c for c in ch.query_all_calls if "cosineDistance" in c[0]]
        assert vec, "no vector leg issued"
        sql, params = vec[0]
        assert "hasAny(e.tags, ['sensitivity:medium', 'sensitivity:high'])" in sql
        assert "{sensitivity_penalty:Float32}" in sql and params["sensitivity_penalty"] == 0.1
        ch = StubCH()
        ch.query_all_returns.append([])
        q = dataclasses.replace(_boosted_semantic_q(), sensitivity_penalty=0.0, search_mode=mode)
        await vector_search(q, ch=ch)
        vec = [c for c in ch.query_all_calls if "cosineDistance" in c[0]]
        assert vec and "sensitivity:medium" not in vec[0][0]


def test_rrf_adaptive_lexical_is_on_unless_the_request_pins_it_off() -> None:
    """None/True = adaptive; False = equal weights for this call."""
    import dataclasses

    from server.memory.app.retrieval import fusion as ms

    q = dataclasses.replace(
        _boosted_semantic_q(),
        search_mode="rrf",
        query_text="src/server/memory/app/_memory_search.py:1234 fix_ranking_v2",
    )
    code_shape = ms._rrf_code_shape(q.query_text)
    assert code_shape > 0.3, code_shape  # a code/path-shaped query
    assert ms._rrf_lexical_weight(q) < 1.0  # unpinned → adaptive
    assert ms._rrf_lexical_weight(dataclasses.replace(q, rrf_adaptive_lexical=True)) < 1.0
    assert ms._rrf_lexical_weight(dataclasses.replace(q, rrf_adaptive_lexical=False)) == 1.0


# ---- key-embedding axis -------------------------------------


@pytest.mark.asyncio
async def test_key_axis_off_never_names_the_key_vector_column() -> None:
    """Off, `distance` is the bare value distance and memory_key_embedding is
    absent from the SQL, so ClickHouse never reads that column; ordering
    still uses the bare value_distance alias the codes-first read needs."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(search_mode="semantic", key_axis=False), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "memory_key_embedding" not in sql
    assert "least(" not in sql.split("AS distance")[0]
    assert "cosineDistance(e.embedding, {q:Array(Float32)}) AS distance" in sql
    assert "ORDER BY value_distance ASC" in sql


@pytest.mark.asyncio
async def test_key_axis_defaults_to_the_setting(monkeypatch) -> None:
    from server.settings import settings

    ch = StubCH()
    ch.query_all_returns.append([])
    monkeypatch.setattr(settings, "memory_search_key_axis", "off")
    await vector_search(_q(search_mode="semantic"), ch=ch)
    assert "memory_key_embedding" not in ch.query_all_calls[0][0]
    ch = StubCH()
    ch.query_all_returns.append([])
    monkeypatch.setattr(settings, "memory_search_key_axis", "on")
    await vector_search(_q(search_mode="semantic"), ch=ch)
    assert "memory_key_embedding" in ch.query_all_calls[0][0]
    # an explicit request beats the setting
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(search_mode="semantic", key_axis=True), ch=ch)
    assert "memory_key_embedding" in ch.query_all_calls[0][0]


@pytest.mark.asyncio
async def test_reinforcement_half_life_grows_with_use_only_when_scaled(monkeypatch) -> None:
    """Scale 0 is the fixed half-life byte-for-byte; a positive
    scale multiplies the half-life by (1 + scale · log1p(count − 1))."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(dataclasses.replace(_boosted_semantic_q(), search_mode="hybrid"), ch=ch)
    sql, params = ch.query_all_calls[0]
    expr = _rank_score_expr(sql)
    assert "reinf_use_scale" not in expr and "reinf_use_scale" not in params
    assert "greatest({reinf_halflife:Float32}, 1.0)" in expr

    ch2 = StubCH()
    ch2.query_all_returns.append([])
    q2 = dataclasses.replace(
        _boosted_semantic_q(), search_mode="hybrid", reinforcement_half_life_use_scale=0.5
    )
    await vector_search(q2, ch=ch2)
    sql2, params2 = ch2.query_all_calls[0]
    expr2 = _rank_score_expr(sql2)
    assert params2["reinf_use_scale"] == 0.5
    assert (
        "{reinf_halflife:Float32} * (1.0 + {reinf_use_scale:Float32} "
        "* log(1.0 + greatest(toFloat32(e.reinforcement_count) - 1.0, 0.0)))"
    ) in expr2


def test_half_life_use_scale_resolves_from_the_request() -> None:
    from types import SimpleNamespace as NS

    from server.memory.app.search.knobs import _reinforcement_half_life_use_scale

    assert _reinforcement_half_life_use_scale(NS(reinforcement_half_life_use_scale=0.3)) == 0.3
    assert _reinforcement_half_life_use_scale(NS(reinforcement_half_life_use_scale=None)) == 0.0


async def test_graph_mode_structural_boosts_ride_under_the_prior_cap() -> None:
    """In graph mode the entity-overlap + triple boosts are one
    group bounded by prior_cap, so structure reorders near-ties and cannot
    displace a materially closer memory; prior_cap 0 keeps the legacy form."""
    import dataclasses

    base = dataclasses.replace(
        _boosted_semantic_q(),
        search_mode="graph",
        graph_expanded=["caroline", "pride"],
        triple_matched_keys=["k1"],
    )
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(dataclasses.replace(base, prior_cap=0.08), ch=ch)
    sql, params = ch.query_all_calls[0]
    expr = _rank_score_expr(sql)
    assert "least({prior_cap:Float32}, (" in expr
    assert "arrayIntersect(e.entities" in expr and "triple_keys" in expr
    assert params["prior_cap"] == 0.08
    # the query-entity boost (0.5 × overlap with {ents}) is INSIDE the capped
    # group, not a free term beside cosine.
    inside = expr[expr.index("least({prior_cap:Float32}, (") :]
    inside = inside[: inside.index("+ if(has(e.tags")] if "+ if(has(e.tags" in inside else inside
    assert "{ents:Array(String)}" in inside
    head = expr[: expr.index("least({prior_cap:Float32}, (")]
    assert "{ents:Array(String)}" not in head

    ch2 = StubCH()
    ch2.query_all_returns.append([])
    await vector_search(dataclasses.replace(base, prior_cap=0.0), ch=ch2)
    sql2, _ = ch2.query_all_calls[0]
    assert "least({prior_cap:Float32}, (" not in _rank_score_expr(sql2)
    assert "arrayIntersect(e.entities" in _rank_score_expr(sql2)


@pytest.mark.asyncio
async def test_graph_mode_ranks_bare_cosine_plus_capped_structure(monkeypatch) -> None:
    """Graph mode keeps cosine, the capped
    structural group and the correctness tail — no recency / preference /
    chronological / temporal-anchor boosts and no prior group."""
    import dataclasses

    q = dataclasses.replace(
        _boosted_semantic_q(),
        search_mode="graph",
        graph_expanded=["caroline"],
        triple_matched_keys=["k1"],
        prior_cap=0.08,
    )
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(q, ch=ch)
    sql, params = ch.query_all_calls[0]
    # graph mode emits component columns (preference_boost_sql, …) between
    # lexical_sql and rank_score, so isolate the rank expression itself: the
    # last "((1.0 - distance)" before "AS rank_score".
    head = sql.split("AS rank_score,", 1)[0]
    expr = head[head.rfind("((1.0 - distance)") :]
    assert "least({prior_cap:Float32}, (" in expr
    for name in (
        "recency_boost",
        "preference (pref_fallback)",
        "confidence prior",
        "reinforcement_boost",
    ):
        assert _BOOST_FRAGMENTS[name] not in expr, name
    assert "e.status = 'conflicted'" in expr  # correctness tail kept
    assert "confidence_weight" not in params


@pytest.mark.asyncio
async def test_graph_mode_sorts_the_fused_pool_by_rank(monkeypatch) -> None:
    """Graph (and temporal) mode sorts the fused pool by rank like semantic:
    in gather order (facts first, the chunk leg's rows after) top_k would never
    reach a chunk however it scored."""
    import dataclasses

    q = dataclasses.replace(
        _boosted_semantic_q(),
        search_mode="graph",
        top_k=2,
        execution_planes=["fact", "graph"],
        graph_expanded=["caroline"],
    )
    ch = StubCH()
    # ANN rows arrive low rank first; the slice must still return the best two.
    ch.query_all_returns.append(
        [
            {
                "memory_key": "low",
                "memory_type": "semantic",
                "content_excerpt": "l",
                "distance": 0.6,
                "rank_score": 0.40,
                "lex_score": 0.0,
                "temporal_anchor": "",
                "temporal_confidence": 0.0,
                "valid_at": "",
                "source_resource_id": "",
                "source_class": "",
                "verified": 0,
                "session_id": "",
                "entity_boost_sql": 0.0,
                "lexical_sql": 0.0,
            },
            {
                "memory_key": "mid",
                "memory_type": "semantic",
                "content_excerpt": "m",
                "distance": 0.4,
                "rank_score": 0.60,
                "lex_score": 0.0,
                "temporal_anchor": "",
                "temporal_confidence": 0.0,
                "valid_at": "",
                "source_resource_id": "",
                "source_class": "",
                "verified": 0,
                "session_id": "",
                "entity_boost_sql": 0.0,
                "lexical_sql": 0.0,
            },
            {
                "memory_key": "high",
                "memory_type": "semantic",
                "content_excerpt": "h",
                "distance": 0.2,
                "rank_score": 0.80,
                "lex_score": 0.0,
                "temporal_anchor": "",
                "temporal_confidence": 0.0,
                "valid_at": "",
                "source_resource_id": "",
                "source_class": "",
                "verified": 0,
                "session_id": "",
                "entity_boost_sql": 0.0,
                "lexical_sql": 0.0,
            },
        ]
    )
    hits = await vector_search(q, ch=ch)
    assert [h.memory_key for h in hits] == ["high", "mid"]


@pytest.mark.asyncio
async def test_a_record_gives_way_to_a_stronger_fact_of_its_key_whoever_wrote_it() -> None:
    # a record's identity is what memory.record_write was given, never the fact row's
    ch = StubCH()
    fact = {
        **_hit(),
        "memory_key": "profile_theme",
        "distance": 0.05,
        "agent": "bot",
        "user_id": "u",
        "session_id": "s1",
    }
    ch.query_all_returns.extend(
        [
            [fact],
            [
                {
                    "record_id": "11111111-2222-3333-4444-555555555555",
                    "parent_memory_key": "profile_theme",
                    "schema_name": "preference",
                    "record_json": '{"theme": "dark"}',
                    "field_text": "theme: dark",
                    "lexical_score": 1.0,
                    "rank_score": 0.3,
                    "session_id": "-",
                }
            ],
        ]
    )
    out = await vector_search(
        _q(query_text="what theme do I use", execution_planes=["fact", "record"]), ch=ch
    )
    assert [(h.memory_key, h.source) for h in out] == [("profile_theme", "fact")]


def test_a_record_hit_names_no_memory_row() -> None:
    from server.memory.app.retrieval.side_legs import _record_hits

    (hit,) = _record_hits(
        [
            {
                "record_id": "r1",
                "parent_memory_key": "k",
                "session_id": "s1",
            }
        ]
    )
    # read and keyed like any hit that names no row; its session stays as provenance
    assert (hit.identity, hit.session_id) == (None, "s1")


@pytest.mark.asyncio
async def test_two_writers_rows_of_a_key_whose_excerpts_open_alike_both_stay() -> None:
    ch = StubCH()
    row = {**_hit(), "session_id": "s1"}
    ch.query_all_returns.append(
        [{**row, "agent": "a", "user_id": "u1"}, {**row, "agent": "a", "user_id": "u2"}]
    )
    out = await vector_search(_q(), ch=ch)
    assert sorted(h.identity for h in out) == [("a", "u1", "s1"), ("a", "u2", "s1")]


@pytest.mark.asyncio
async def test_a_chunk_is_dropped_only_for_a_fact_of_its_own_writer() -> None:
    from dataclasses import replace

    from server.memory.app.retrieval.search import _merge_leg_hits
    from server.memory.domain.search_types import SearchHit

    text = "the same long value both users stored under one key " * 3
    fact = SearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt=text,
        distance=0.2,
        identity=("a", "u1", "s1"),
    )
    chunk = replace(fact, memory_key="chunk:s9:0", source="chunk", identity=("a", "u2", "s9"))
    own = replace(chunk, memory_key="chunk:s8:0", identity=("a", "u1", "s8"))
    out = await _merge_leg_hits(StubCH(), _q(), [fact], [chunk, own], [], [])
    assert [h.memory_key for h in out] == ["k", "chunk:s9:0"]
