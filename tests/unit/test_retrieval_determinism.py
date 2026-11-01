"""A memory search is a function of its inputs.

Every ranked list of hits sorts by (−rank_score, distance, memory_key); the
non-vector SQL legs carry a key tie-breaker after their score; the two vector
legs keep the bare `ORDER BY distance` shape the codes-first read requires and
leave determinism to the Python sort over their over-fetched candidates.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain.search_types import hit_sort_key
from tests.unit.test_memory_search_service import StubCH, _q


def _h(key: str, score: float, dist: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, rank_score=score, distance=dist)


def test_sort_key_breaks_score_ties_by_distance_then_key() -> None:
    hits = [_h("b", 0.9, 0.20), _h("a", 0.9, 0.20), _h("c", 0.9, 0.10), _h("d", 0.95, 0.9)]
    for _ in range(20):
        random.shuffle(hits)
        ordered = sorted(hits, key=hit_sort_key)
        assert [h.memory_key for h in ordered] == ["d", "c", "a", "b"]


def test_sort_key_tolerates_missing_fields() -> None:
    assert hit_sort_key(SimpleNamespace(memory_key="k")) == (-0.0, 0.0, "k", "", "", "")
    assert hit_sort_key(SimpleNamespace(rank_score=None, distance=None, memory_key=None)) == (
        -0.0,
        0.0,
        "",
        "",
        "",
        "",
    )


@pytest.mark.asyncio
async def test_lexical_union_and_trace_legs_carry_a_key_tie_breaker() -> None:
    from server.memory.app.retrieval import vector_search_trace
    from server.memory.app.retrieval.side_legs import _lexical_recall_fallback

    ch = StubCH()
    ch.query_all_returns.append([])
    await _lexical_recall_fallback(ch, _q(query_text="deploy window"))
    sql, _ = ch.query_all_calls[0]
    assert "ORDER BY lex_score DESC, distance ASC, e.memory_key ASC" in sql

    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search_trace(_q(entity_filter=["Alice"]), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "ORDER BY rank_score DESC, distance ASC, e.memory_key ASC" in sql


@pytest.mark.asyncio
async def test_chunk_stage_two_carries_a_tie_breaker_and_stage_one_stays_codes_first_shaped() -> (
    None
):
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append([{"session_id": "s1", "chunk_index": 3, "distance": 0.2}])
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="binoculars", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    sql1, _ = ch.query_all_calls[0]
    sql2, _ = ch.query_all_calls[1]
    assert (
        "ORDER BY distance ASC\n" in sql1 and "session_id ASC" not in sql1
    )  # bare: the codes-first shape
    assert "ORDER BY distance ASC, c.session_id ASC, c.chunk_index ASC" in sql2  # PK re-read: free


@pytest.mark.asyncio
async def test_main_vector_leg_keeps_the_bare_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.retrieval import vector_search
    from server.settings import settings

    # "auto" would probe key reuse through the stub first; pin the axis
    monkeypatch.setattr(settings, "memory_search_key_axis", "off")
    ch = StubCH()
    # first call = ANN read returns one candidate; the other legs get []
    ch.query_all_returns.append(
        [
            {
                "memory_key": "k1",
                "memory_type": "semantic",
                "content_excerpt": "x",
                "distance": 0.2,
                "rank_score": 0.8,
                "value_distance": 0.2,
            }
        ]
    )
    await vector_search(_q(scope="private", agent="a", execution_planes=["fact"]), ch=ch)
    sqls = [s for s, _ in ch.query_all_calls]
    main = [s for s in sqls if "{internal_limit:UInt32}" in s]
    assert main and "ORDER BY value_distance ASC\n" in main[0]
    assert "value_distance ASC, e.memory_key" not in main[0]  # the codes-first shape


def test_python_fusion_sorts_use_the_shared_key() -> None:
    from pathlib import Path

    from server.memory.app import retrieval as service
    from tests.unit._memory_search_pkg import handler_source

    service_files = sorted(Path(service.__file__).parent.glob("*.py"))
    sources = {"server.memory.app.search": handler_source()}
    sources.update({f"{service.__name__}.{p.stem}": p.read_text() for p in service_files})
    for name, src in sources.items():
        assert "sort(key=lambda h: -h.rank_score)" not in src, name
        assert "sort(key=lambda h: (-h.rank_score, h.distance))" not in src, name


def test_bench_helpers_score_agreement() -> None:
    from bench.retrieval_determinism import summarise, top_keys

    out = {"results": [{"memory_key": "a"}, {"memory_key": "b"}, {"memory_key": "c"}]}
    assert top_keys(out, 2) == ["a", "b"]
    rep = summarise(
        {
            "q1": [["a", "b"], ["a", "b"], ["a", "b"]],
            "q2": [["a", "b"], ["b", "a"]],
            "q3": [["a", "b"], ["a", "c"]],
        },
        k=2,
        repeats=3,
    )
    assert rep.share("same_order") == round(1 / 3, 4)
    assert rep.share("same_set") == round(2 / 3, 4)
    assert rep.share("same_top1") == round(2 / 3, 4)
    assert rep.results[2].mean_jaccard == round(1 / 3, 4)
    j: dict[str, Any] = rep.to_json()
    assert j["queries"] == 3 and len(j["unstable"]) == 1


def test_two_rows_of_one_key_with_equal_scores_sort_by_their_writer() -> None:
    # one key holds a row per writer and session: the row settles the tie
    def row(user: str) -> SimpleNamespace:
        return SimpleNamespace(
            memory_key="k", rank_score=0.5, distance=0.5, identity=("a", user, "s1")
        )

    first, second = row("u1"), row("u2")
    assert sorted([second, first], key=hit_sort_key) == [first, second]
    assert sorted([first, second], key=hit_sort_key) == [first, second]
