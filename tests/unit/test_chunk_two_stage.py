"""The chunk leg is indexed and re-read like the fact leg.

Stage 1 must be the one query shape the vector index serves (bare ORDER BY
cosineDistance LIMIT, no FINAL, no JOIN, identities only); stage 2 must be
the full query as a primary-key lookup over those identities under FINAL
with skip indexes off. The legacy single query stays behind the flag.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.test_memory_search_service import StubCH, _q


def _cands(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [
        {"session_id": s, "chunk_index": i, "distance": 0.1 * (k + 1)}
        for k, (s, i) in enumerate(pairs)
    ]


def test_stage_one_is_the_shape_the_index_serves() -> None:
    from server.memory.app.retrieval.chunks import _chunk_candidates_sql

    sql = _chunk_candidates_sql("AND c.agent = {agent:String}")
    assert "FROM loom.memory_chunks AS c\n" in sql or "FROM loom.memory_chunks AS c " in sql
    assert " FINAL" not in sql
    assert "JOIN" not in sql
    assert "ORDER BY distance ASC" in sql and "LIMIT {chunk_limit:UInt32}" in sql
    assert "vector_search_index_fetch_multiplier" in sql
    for pred in (
        "c.org = {org:String}",
        "c.namespace = {ns:String}",
        "c.embedding_model = {model:String}",
        "c.tombstoned_at IS NULL",
        "AND c.agent = {agent:String}",
    ):
        assert pred in sql
    # identities and distance only — the expensive columns wait for stage 2
    select = sql.split("FROM", 1)[0]
    assert "c.content" not in select  # the passage text waits for stage 2


@pytest.mark.asyncio
async def test_stage_two_is_a_pk_lookup_under_final_over_the_candidates() -> None:
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands(("s1", 3), ("s2", 0), ("s1", 4)))
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="binoculars", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})

    assert len(ch.query_all_calls) == 2
    sql1, p1 = ch.query_all_calls[0]
    sql2, p2 = ch.query_all_calls[1]
    assert " FINAL" not in sql1 and "FROM loom.memory_chunks AS c FINAL" in sql2
    assert (
        "(c.session_id, c.chunk_index) IN arrayZip({cand_sess:Array(String)}, {cand_idx:Array(UInt32)})"
        in sql2
    )
    assert "use_skip_indexes = 0" in sql2
    assert ") AS pd ON pd.pd_memory_key = c.parent_memory_key" in sql2  # parent dates still joined
    # stage 1 over-fetches 2x; stage 2 keeps the caller's limit
    assert p1["chunk_limit"] == p2["chunk_limit"] * 2
    assert p2["cand_sess"] == ["s1", "s2", "s1"] and p2["cand_idx"] == [3, 0, 4]


@pytest.mark.asyncio
async def test_stage_two_never_reads_the_chunk_vector() -> None:
    """Stage 2 looks each candidate's distance up by identity;
    the 6 KB `c.embedding` column is read once, in stage 1."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands(("s1", 3), ("s2", 0)))
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="binoculars", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})

    sql1, _ = ch.query_all_calls[0]
    sql2, p2 = ch.query_all_calls[1]
    assert "cosineDistance(c.embedding" in sql1
    # The statement is a union; the candidate branch (before
    # UNION ALL) still never reads the vector — only the ±1 window branch,
    # a handful of rows, computes an exact distance.
    cand_branch = sql2.split("UNION ALL", 1)[0]
    assert "cosineDistance(c.embedding" not in cand_branch and "c.embedding," not in cand_branch
    assert "cosineDistance(c.embedding" in sql2.split("UNION ALL", 1)[1]
    assert (
        "transform(concat(toString(c.session_id), '\\x1f', toString(c.chunk_index)), "
        "{cand_ident:Array(String)}, {cand_dist:Array(Float32)}, 1.0) AS distance" in cand_branch
    )
    assert p2["cand_ident"] == ["s1\x1f3", "s2\x1f0"]
    assert p2["cand_dist"] == [pytest.approx(0.1), pytest.approx(0.2)]
    # One parent-date row per memory_key, so the
    # LIMIT counts chunks, not (chunk × parent rows).
    pd_sub = sql2[sql2.index("SELECT memory_key AS pd_memory_key") : sql2.index(") AS pd ON")]
    assert "GROUP BY memory_key" in pd_sub
    assert "max(temporal_anchor) AS temporal_anchor" in pd_sub


@pytest.mark.asyncio
async def test_empty_candidates_skip_stage_two() -> None:
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="nothing", top_k=5)
    out = await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert out == [] and len(ch.query_all_calls) == 1


@pytest.mark.asyncio
async def test_stage_one_failure_is_logged_and_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from server.memory.app.retrieval.chunks import _search_memory_chunks

    class BoomCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("Code 48")

    q = _q(entity_filter=[], query_text="x", top_k=5)
    with caplog.at_level(logging.ERROR, logger="server.memory.app.retrieval"):
        out = await _search_memory_chunks(BoomCH(), q, {"q": q.query_vec})
    assert out == []
    assert any("stage=candidates" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_run_scope_reaches_both_chunk_stages() -> None:
    """A run-scoped search filters the chunk leg on run_id in
    the index stage and the FINAL stage, mirroring the fact leg."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands(("s1", 3)))
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="plan", top_k=5, run_id="run-7")
    await _search_memory_chunks(ch, q, {"q": q.query_vec})

    (sql1, p1), (sql2, p2) = ch.query_all_calls
    assert "AND c.run_id = {run_id:String}" in sql1
    assert "AND c.run_id = {run_id:String}" in sql2
    assert p1["run_id"] == "run-7" and p2["run_id"] == "run-7"


@pytest.mark.asyncio
async def test_unscoped_search_reads_every_runs_chunks() -> None:
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands(("s1", 3)))
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="plan", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    (sql1, p1), (sql2, _p2) = ch.query_all_calls
    assert "c.run_id" not in sql1 and "c.run_id" not in sql2 and "run_id" not in p1


def _stage2_row(sess: str, idx: int, parent: str = "p1") -> dict[str, Any]:
    return {
        "memory_key": f"chunk:{sess}:{idx}",
        "memory_type": "episodic",
        "content_excerpt": "we bought binoculars for the trip",
        "content_full": "we bought binoculars for the trip",
        "distance": 0.2,
        "session_id": sess,
        "chunk_index": idx,
        "parent_memory_key": parent,
        "entity_overlap_count": 0,
        "temporal_anchor": "2024-05-01T00:00:00",
        "valid_at": "2024-05-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_neighbor_window_predicates_are_table_qualified_and_deduped() -> None:
    """The separate neighbour-window statement (for anchors the stage-2
    prefetch did not cover) joins the two date subqueries; its session/index
    predicates name the chunk table so no joined column can make them
    ambiguous, and one repeated anchor yields one OR arm."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    # eight candidates: the prefetch covers the six closest
    ch.query_all_returns.append(
        _cands_with_parent(*[(f"s{i}", i, 0.1 + i / 100.0, "p") for i in range(1, 9)])
    )
    # after the FINAL collapse the top anchors are s8:8 (twice: one row per
    # parent fact) and s7:7, neither prefetched
    ch.query_all_returns.append(
        [
            _union_row("s8", 8, 0.10, neighbor=False, parent="p1"),
            _union_row("s8", 8, 0.10, neighbor=False, parent="p2"),
            _union_row("s7", 7, 0.20, neighbor=False),
        ]
    )
    ch.query_all_returns.append([])  # neighbor window
    q = _q(entity_filter=[], query_text="binoculars trip", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})

    assert len(ch.query_all_calls) == 3
    sql3, p3 = ch.query_all_calls[2]
    arm0 = "(c.session_id = {ns0:String}  AND c.chunk_index BETWEEN {lo0:UInt32}  AND {hi0:UInt32})"
    assert arm0 in sql3
    assert "(c.session_id = {ns1:String}" in sql3 and "{ns2:String}" not in sql3  # deduped
    assert p3["ns0"] == "s8" and p3["lo0"] == 7 and p3["hi0"] == 9
    assert p3["ns1"] == "s7" and p3["lo1"] == 6 and p3["hi1"] == 8
    # the date joins are present and expose no bare session_id / memory_key
    assert ") AS ss ON ss.ss_session_id = c.session_id" in sql3
    assert ") AS pd ON pd.pd_memory_key = c.parent_memory_key" in sql3
    assert "(session_id = " not in sql3 and " AND chunk_index BETWEEN" not in sql3


# --------------------------------------------------------------------------
# The chunk leg in one round trip after stage 1.
# --------------------------------------------------------------------------


def _cands_with_parent(*pairs: tuple[str, int, float, str]) -> list[dict[str, Any]]:
    return [
        {"session_id": s, "chunk_index": i, "distance": d, "parent_memory_key": p}
        for s, i, d, p in pairs
    ]


def _union_row(
    sess: str, idx: int, dist: float, *, neighbor: bool, parent: str = "p1"
) -> dict[str, Any]:
    r = _stage2_row(sess, idx, parent)
    r["distance"] = dist
    r["is_neighbor"] = 1 if neighbor else 0
    r["content_full"] = r["content_excerpt"] = f"binoculars for the trip {sess} {idx}"
    return r


def test_stage_one_inherits_the_multiplier_and_carries_no_thread_pin() -> None:
    """Stage 1 carries the deployment's vector multiplier, and no `max_threads`
    pin on a no-FINAL scan (the threads follow the marks)."""
    from server.memory.app.retrieval.chunks import _chunk_candidates_sql

    sql = _chunk_candidates_sql("")
    assert "vector_search_index_fetch_multiplier = " in sql
    assert "max_threads" not in sql
    assert "c.parent_memory_key AS parent_memory_key" in sql.split("FROM", 1)[0]
    assert "c.content" not in sql.split("FROM", 1)[0]


def test_anchor_prefetch_is_stage_twos_order_over_distinct_identities() -> None:
    from server.memory.app.retrieval.chunks import (
        _CHUNK_ANCHOR_PREFETCH,
        _chunk_anchor_prefetch,
        _chunk_window_idents,
    )

    cands = _cands_with_parent(
        ("s2", 5, 0.30, "p"),
        ("s1", 3, 0.10, "p"),
        ("s1", 3, 0.12, "p"),  # two versions
        ("s3", 0, 0.10, "p"),
        ("s4", 9, 0.20, "p"),
        ("s5", 1, 0.25, "p"),
        ("s6", 2, 0.26, "p"),
        ("s7", 7, 0.27, "p"),
    )
    got = _chunk_anchor_prefetch(cands)
    assert got == [("s1", 3), ("s3", 0), ("s4", 9), ("s5", 1), ("s6", 2), ("s7", 7)]
    assert len(got) == _CHUNK_ANCHOR_PREFETCH
    assert _chunk_window_idents([("s1", 0), ("s1", 1)]) == [("s1", 0), ("s1", 1), ("s1", 2)]


@pytest.mark.asyncio
async def test_one_trip_stage_two_carries_the_windows_and_keyed_dates() -> None:
    """Stage 2 is one statement: the candidate re-read (its LIMIT and, when
    set, the entity-overlap join inside its own branch), UNION ALL the ±1
    windows of stage 1's best chunks with their exact distance, dated once
    by joins keyed on the parents stage 1 returned, the window rows'
    parents (a narrow PK read) and the sessions involved."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(
        _cands_with_parent(("s1", 3, 0.1, "pa"), ("s2", 0, 0.2, "pb"), ("s1", 4, 0.3, "pa"))
    )
    ch.query_all_returns.append([])
    q = _q(entity_filter=["nate"], query_text="binoculars trip", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})

    assert len(ch.query_all_calls) == 2
    sql, p = ch.query_all_calls[1]
    cand_branch, nb_branch = sql.split("UNION ALL", 1)
    assert cand_branch.count("FROM loom.memory_chunks AS c FINAL") == 1
    assert (
        "ORDER BY distance ASC, c.session_id ASC, c.chunk_index ASC,\n"
        "                     c.created_at DESC, c.content_sha256 ASC" in cand_branch
    )
    assert "LIMIT {chunk_limit:UInt32})" in cand_branch
    assert (
        ") AS eo ON eo.memory_key = c.parent_memory_key" in cand_branch
    )  # fan-out kept where it was
    assert (
        "(c.session_id, c.chunk_index) IN arrayZip({cand_sess:Array(String)}, {cand_idx:Array(UInt32)})"
        in cand_branch
    )
    assert (
        "(c.session_id, c.chunk_index) IN arrayZip({nb_sess:Array(String)}, {nb_idx:Array(UInt32)})"
        in nb_branch
    )
    assert "cosineDistance(c.embedding, {q:Array(Float32)}) AS distance" in nb_branch
    assert "1 AS is_neighbor" in nb_branch and "0 AS is_neighbor" in cand_branch
    # the identity predicate scopes both branches, plain WHERE, no PREWHERE
    assert (
        cand_branch.count("c.tombstoned_at IS NULL") == 1 and "c.tombstoned_at IS NULL" in nb_branch
    )
    assert "PREWHERE" not in sql
    # dates joined once, outside the union, keyed
    tail = sql.split(") AS c", 1)[1]
    assert ") AS ss ON ss.ss_session_id = c.session_id" in tail
    assert ") AS pd ON pd.pd_memory_key = c.parent_memory_key" in tail
    assert "session_id IN {all_sess:Array(String)}" in tail
    assert "memory_key IN {pd_keys:Array(String)}" in tail
    assert "SELECT parent_memory_key\n                       FROM loom.memory_chunks" in tail
    assert tail.count("GROUP BY memory_key") == 1
    assert "SETTINGS use_skip_indexes = 0, optimize_move_to_prewhere_if_final = 1" in tail
    # parameters: windows of the (here three) best distinct chunks, sorted keys
    assert p["nb_sess"] == ["s1", "s1", "s1", "s2", "s2", "s1"] and p["nb_idx"] == [
        2,
        3,
        4,
        0,
        1,
        5,
    ]
    assert p["pd_keys"] == ["pa", "pb"] and p["all_sess"] == ["s1", "s2"]
    assert p["chunk_limit"] == 15 and ch.query_all_calls[0][1]["chunk_limit"] == 30


@pytest.mark.asyncio
async def test_one_trip_without_query_words_reads_no_windows() -> None:
    """No alpha token of three letters = no neighbour expansion (as before),
    so the statement has one branch and the parent read is keyed by the
    candidates' parents alone."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands_with_parent(("s1", 3, 0.1, "pa")))
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="42", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    sql, p = ch.query_all_calls[1]
    assert "UNION ALL" not in sql and "is_neighbor" in sql
    assert "nb_sess" not in p and "SELECT parent_memory_key" not in sql
    assert "memory_key IN {pd_keys:Array(String)}" in sql and p["pd_keys"] == ["pa"]


@pytest.mark.asyncio
async def test_one_trip_neighbors_come_from_the_window_rows() -> None:
    """The window rows of the chosen anchors become neighbour hits scored
    cosine + density (below their anchors); a window row of a prefetched
    chunk that did not become an anchor is dropped; anchors dedup against
    themselves; candidate rows keep stage 2's order for the anchor choice."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(
        _cands_with_parent(
            ("s1", 3, 0.10, "pa"),
            ("s2", 0, 0.20, "pb"),
            ("s3", 5, 0.25, "pc"),
            ("s4", 8, 0.40, "pd"),
        )
    )
    ch.query_all_returns.append(
        [
            # the union comes back unordered
            _union_row("s3", 5, 0.25, neighbor=False, parent="pc"),
            _union_row("s1", 3, 0.10, neighbor=False, parent="pa"),
            _union_row("s2", 0, 0.20, neighbor=False, parent="pb"),
            _union_row("s4", 8, 0.40, neighbor=False, parent="pd"),
            _union_row("s1", 2, 0.50, neighbor=True),  # window of anchor s1:3
            _union_row("s1", 3, 0.10, neighbor=True),  # the anchor itself: already seen
            _union_row("s2", 1, 0.55, neighbor=True),  # window of anchor s2:0
            _union_row("s4", 7, 0.30, neighbor=True),  # window of s4:8 — rank 4, not an anchor
        ]
    )
    q = _q(entity_filter=[], query_text="binoculars trip", top_k=5)
    out = await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 2  # no third statement
    keys = [h.memory_key for h in out]
    assert "chunk:s1:2" in keys and "chunk:s2:1" in keys
    assert "chunk:s4:7" not in keys
    assert keys.count("chunk:s1:3") == 1
    nb = {h.memory_key: h for h in out if "chunk_parent_boost" not in h.score_components}
    # cosine + density (two query tokens present in the text: 0.04)
    assert nb["chunk:s1:2"].rank_score == pytest.approx((1.0 - 0.5) + 0.04)
    assert nb["chunk:s1:2"].distance == 0.5
    anchors = [h for h in out if "chunk_parent_boost" in h.score_components]
    assert [h.memory_key for h in anchors][:2] == ["chunk:s1:3", "chunk:s2:0"]


@pytest.mark.asyncio
async def test_anchor_outside_the_prefetch_falls_back_to_the_window_statement() -> None:
    """When stage 2's top three includes a chunk the prefetch did not cover,
    the separate window statement runs for that anchor only — correct, one
    round trip slower — and its rows merge with the prefetched windows."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    cands = _cands_with_parent(*[(f"s{i}", i, 0.1 + i / 100.0, "p") for i in range(1, 9)])
    ch = StubCH()
    ch.query_all_returns.append(cands)
    ch.query_all_returns.append(
        [
            _union_row("s1", 1, 0.11, neighbor=False),
            _union_row("s8", 8, 0.18, neighbor=False),  # 8th of stage 1; rank 2 after collapses
            _union_row("s2", 2, 0.12, neighbor=False),
            _union_row("s1", 0, 0.60, neighbor=True),
        ]
    )
    ch.query_all_returns.append(
        [
            {
                **_union_row("s8", 9, 0.61, neighbor=True),
                "memory_key": "chunk:s8:9",
            }
        ]
    )
    q = _q(entity_filter=[], query_text="binoculars trip", top_k=5)
    out = await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 3
    sql3, p3 = ch.query_all_calls[2]
    assert p3["ns0"] == "s8" and p3["lo0"] == 7 and p3["hi0"] == 9 and "ns1" not in p3
    assert "c.chunk_index BETWEEN {lo0:UInt32}" in sql3 and " FINAL" in sql3
    keys = [h.memory_key for h in out]
    assert "chunk:s1:0" in keys and "chunk:s8:9" in keys


@pytest.mark.asyncio
async def test_private_scope_reaches_the_window_branch_and_the_fallback() -> None:
    """The agent / run predicate scopes the windows like their anchors."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands_with_parent(("s1", 3, 0.1, "pa")))
    ch.query_all_returns.append([_union_row("s9", 9, 0.1, neighbor=False)])  # uncovered anchor
    ch.query_all_returns.append([])
    q = _q(
        entity_filter=[],
        query_text="binoculars trip",
        top_k=5,
        run_id="run-7",
        scope="private",
        agent="bot",
    )
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    sql2, p2 = ch.query_all_calls[1]
    _cand, nb_branch = sql2.split("UNION ALL", 1)
    assert (
        "AND c.agent = {agent:String}" in nb_branch
        and "AND c.run_id = {run_id:String}" in nb_branch
    )
    sql3, p3 = ch.query_all_calls[2]
    assert "AND c.agent = {agent:String}" in sql3 and "AND c.run_id = {run_id:String}" in sql3
    assert p3["agent"] == "bot" and p3["run_id"] == "run-7"
