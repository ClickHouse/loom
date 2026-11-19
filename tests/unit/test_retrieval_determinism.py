"""A memory search is a function of its inputs.

Every ranked list of hits sorts by (−rank_score, distance, memory_key) and then the row's
namespace, agent, user and session; the non-vector SQL legs carry a key tie-breaker after
their score; the two vector legs keep the bare `ORDER BY distance` shape the codes-first
read requires and leave determinism to the Python sort over their over-fetched candidates.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
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
    assert hit_sort_key(SimpleNamespace(memory_key="k")) == (-0.0, 0.0, "k", "", "", "", "")
    assert hit_sort_key(SimpleNamespace(rank_score=None, distance=None, memory_key=None)) == (
        -0.0,
        0.0,
        "",
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


def test_two_sessions_of_one_wire_hit_key_sort_by_their_session() -> None:
    # extraction writes one key per session; a wire hit carries its row in `_row_identity`
    def hit(session: str) -> _MemorySearchHit:
        h = _MemorySearchHit(
            memory_key="k",
            memory_type="semantic",
            content_excerpt="x",
            distance=0.5,
            similarity=0.5,
        )
        h._row_identity = ("a", "u", session)
        return h

    s1, s2 = hit("s1"), hit("s2")
    assert sorted([s2, s1], key=hit_sort_key) == [s1, s2]


def test_one_row_identity_in_two_namespaces_sorts_by_its_namespace() -> None:
    def row(namespace: str) -> SimpleNamespace:
        return SimpleNamespace(
            memory_key="k",
            rank_score=0.5,
            distance=0.5,
            namespace=namespace,
            identity=("a", "u", "s1"),
        )

    n1, n2 = row("n1"), row("n2")
    assert sorted([n2, n1], key=hit_sort_key) == [n1, n2]


def _three_api_snapshot():
    from bench.retrieval_determinism import RANKING_OPS, capture_rankings

    rows = [
        {
            "memory_key": key,
            "memory_type": "semantic",
            "source": "fact",
            "namespace": "ns",
            "distance": distance,
            "similarity": 1 - distance,
            "rank_score": 1 - distance,
        }
        for key, distance in (("harbor", 0.0), ("orchard", 0.75))
    ]
    requests = {
        op: {"quiet harbor": {"query": "quiet harbor", "namespace": "ns"}} for op in RANKING_OPS
    }
    return capture_rankings(
        lambda _op, _body: {"results": rows, "source": "retrieval_only", "synth_calls": 0},
        requests,
        {"quiet harbor": "harbor"},
    )


def test_three_api_snapshot_retains_order_and_all_scores_with_float_tolerance():
    from copy import deepcopy

    from bench.retrieval_determinism import RANKING_OPS, assert_rankings_equal

    before = _three_api_snapshot()
    after = deepcopy(before)
    for op in RANKING_OPS:
        after[op]["quiet harbor"][0]["scores"]["rank_score"] += 1e-8
        assert [row["identity"]["memory_key"] for row in before[op]["quiet harbor"]] == [
            "harbor",
            "orchard",
        ]
        assert set(before[op]["quiet harbor"][0]["scores"]) == {
            "distance",
            "similarity",
            "rank_score",
        }
    assert_rankings_equal(before, after, queries=["quiet harbor"])


@pytest.mark.parametrize(
    "change",
    ["order", "score", "identity", "query", "operation", "empty", "missing-score", "nonfinite"],
)
def test_three_api_comparison_refuses_changed_or_incomplete_results(change):
    from copy import deepcopy

    from bench.retrieval_determinism import assert_rankings_equal

    before = _three_api_snapshot()
    after = deepcopy(before)
    rows = after["memory.search"]["quiet harbor"]
    if change == "order":
        rows.reverse()
    elif change == "score":
        rows[0]["scores"]["rank_score"] += 0.01
    elif change == "identity":
        rows[0]["identity"]["memory_key"] = "another-key"
    elif change == "query":
        after["memory.search"].pop("quiet harbor")
    elif change == "operation":
        after.pop("memory.ask")
    elif change == "empty":
        rows.clear()
    elif change == "missing-score":
        rows[0]["scores"].pop("distance")
    else:
        rows[0]["scores"]["distance"] = float("nan")
    with pytest.raises(AssertionError):
        assert_rankings_equal(before, after, queries=["quiet harbor"])


@pytest.mark.parametrize("invalid", [None, {}, {"results": []}, {"results": [None]}])
def test_three_api_capture_refuses_missing_or_empty_results(invalid):
    from bench.retrieval_determinism import ranked_rows

    with pytest.raises(AssertionError):
        ranked_rows(invalid, namespace="ns", positive_key="harbor")


@pytest.mark.parametrize(
    "field", ["memory_key", "memory_type", "source", "distance", "similarity", "rank_score"]
)
def test_three_api_capture_requires_each_identity_and_score_field(field):
    from bench.retrieval_determinism import ranked_rows

    row = {
        "memory_key": "harbor",
        "memory_type": "semantic",
        "source": "fact",
        "distance": 0.0,
        "similarity": 1.0,
        "rank_score": 1.0,
    }
    row.pop(field)
    with pytest.raises(AssertionError):
        ranked_rows({"results": [row]}, namespace="ns", positive_key="harbor")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), "1.0", True])
def test_three_api_capture_refuses_nonfinite_or_nonnumeric_scores(score):
    from bench.retrieval_determinism import ranked_rows

    row = {
        "memory_key": "harbor",
        "memory_type": "semantic",
        "source": "fact",
        "distance": score,
        "similarity": 1.0,
        "rank_score": 1.0,
    }
    with pytest.raises(AssertionError):
        ranked_rows({"results": [row]}, namespace="ns", positive_key="harbor")


def test_three_api_capture_does_not_omit_a_failed_request():
    from bench.retrieval_determinism import RANKING_OPS, capture_rankings

    requests = {
        op: {"quiet harbor": {"query": "quiet harbor", "namespace": "ns"}} for op in RANKING_OPS
    }

    def refused(_op, _body):
        raise AssertionError("HTTP 429")

    with pytest.raises(AssertionError, match="HTTP 429"):
        capture_rankings(refused, requests, {"quiet harbor": "harbor"})


def test_three_api_capture_requires_its_positive_anchor():
    from bench.retrieval_determinism import ranked_rows

    row = {
        "memory_key": "another-key",
        "memory_type": "semantic",
        "source": "fact",
        "distance": 0.0,
        "similarity": 1.0,
        "rank_score": 1.0,
    }
    with pytest.raises(AssertionError, match="positive anchor"):
        ranked_rows({"results": [row]}, namespace="ns", positive_key="harbor")


def test_three_api_timings_report_small_sample_median_maximum_and_each_call():
    from bench.retrieval_determinism import RANKING_OPS, ranking_timings

    calls = {op: {"first": 0.03, "second": 0.01, "third": 0.02} for op in RANKING_OPS}
    report = ranking_timings(calls)
    for op in RANKING_OPS:
        assert report[op] == {
            "n": 3,
            "p50_s": 0.02,
            "p99_s": 0.03,
            "calls_s": calls[op],
        }
