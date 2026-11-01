"""The latest run is the latest.

On a recency-intent query the fused pool is re-ranked by date rank when the
namespace's retrieval defaults (or the request) set `recency_pool_weight`:
the newest dated hit gains the weight, the oldest nothing, undated hits
nothing. Off by default at the deployment; a namespace declares it once.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.memory.app.retrieval.pool import recency_pool_scores, rerank_by_recency_pool
from server.memory.domain.search_types import SearchHit


def _hit(key: str, score: float, valid_at: str = "", anchor: str = "") -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=1.0 - score,
        rank_score=score,
        valid_at=valid_at,
        temporal_anchor=anchor,
    )


def test_scores_are_rank_relative_and_ignore_undated() -> None:
    hits = [
        _hit("a", 0.50, "2026-06-01 10:00:00"),
        _hit("b", 0.49, "2026-08-25T12:03:38"),  # ISO 'T' form normalises
        _hit("c", 0.48, ""),  # undated
        _hit("d", 0.47, "2026-07-15 00:00:00"),
        _hit("e", 0.46, "1970-01-01 00:00:00"),  # the chunk leg's undated sentinel
    ]
    s = recency_pool_scores(hits, weight=0.3)
    assert s[1] == pytest.approx(0.3) and s[3] == pytest.approx(0.15)  # newest, then half
    assert s[0] == pytest.approx(0.075) and s[2] == 0.0 and s[4] == 0.0


def test_shared_dates_share_a_rank_and_one_date_is_no_order() -> None:
    same = [_hit("a", 0.5, "2026-08-01 00:00:00"), _hit("b", 0.4, "2026-08-01 00:00:00")]
    assert recency_pool_scores(same, weight=0.3) == [0.0, 0.0]
    three = same + [_hit("c", 0.3, "2026-07-01 00:00:00")]
    assert recency_pool_scores(three, weight=0.3) == [0.3, 0.3, 0.15]
    assert recency_pool_scores(three, weight=0.0) == [0.0, 0.0, 0.0]


def test_rerank_puts_the_newest_same_template_report_first() -> None:
    # Five reports of one workflow, cosine within 0.03 of each other — the
    # shape of "what did the latest run of X find?" over an investigation store.
    hits = [
        _hit("run-3", 0.61, "2026-07-23 13:12:54"),
        _hit("run-1", 0.60, "2026-07-22 15:50:28"),
        _hit("run-5", 0.59, "2026-08-25 12:03:38"),
        _hit("run-4", 0.585, "2026-08-07 15:08:03"),
        _hit("run-2", 0.58, "2026-07-22 14:50:47"),
    ]
    out = rerank_by_recency_pool(hits, weight=0.3)
    assert [h.memory_key for h in out] == ["run-5", "run-4", "run-3", "run-1", "run-2"]
    assert out[0].score_components["recency_pool"] == pytest.approx(0.3)
    assert out[0].distance == hits[2].distance  # distances untouched
    # nothing dated → the pool comes back as it was
    plain = [_hit("x", 0.5), _hit("y", 0.4)]
    assert rerank_by_recency_pool(plain, weight=0.3) is plain


def test_weight_reaches_the_search_query_from_request_or_default(monkeypatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.knobs import _recency_pool_weight
    from server.settings import settings

    base = {"org": "o", "namespace": "n", "agent": "a", "query": "latest run"}
    monkeypatch.setattr(settings, "memory_search_recency_pool_weight", 0.0)
    assert _recency_pool_weight(MemorySearchInput(**base)) == 0.0
    assert _recency_pool_weight(MemorySearchInput(**base, recency_pool_weight=0.25)) == 0.25
    monkeypatch.setattr(settings, "memory_search_recency_pool_weight", 0.1)
    assert _recency_pool_weight(MemorySearchInput(**base)) == pytest.approx(0.1)


def test_namespace_defaults_carry_the_knob() -> None:
    from server.identity.api.models import NamespaceRetrievalDefaults
    from server.memory.app import namespace_retrieval_defaults as nrd

    assert "recency_pool_weight" in nrd.FIELDS
    parsed = nrd.parse('{"recency_pool_weight": 0.3}')
    assert parsed == {"recency_pool_weight": 0.3}
    assert nrd.as_dict(NamespaceRetrievalDefaults(recency_pool_weight=0.2)) == {
        "recency_pool_weight": 0.2
    }
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(recency_pool_weight=1.5)


def test_the_cue_that_turns_it_on_is_the_parsers_recency_intent() -> None:
    from server.memory.domain.query_intent import _RECENCY_RE

    assert _RECENCY_RE.search("what did the latest run of alert_postmortem find?")
    assert _RECENCY_RE.search("most recent report for cluster-a")
    assert not _RECENCY_RE.search("DC change impact: cluster-a regressed")


# ---- the newest rows in scope join the pool


@pytest.mark.asyncio
async def test_recency_backfill_adds_newest_rows_in_scope_only_when_tagged(monkeypatch) -> None:
    from server.memory.app.retrieval.pool import _recency_backfill
    from tests.unit.test_memory_search_service import StubCH, _q

    pool = [_hit("a", 0.5, "2026-08-01 00:00:00")]
    # tagged + recency intent + weight → one statement, newest first, pool keys excluded
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "memory_key": "w/r9/162194",
                "memory_type": "semantic",
                "content_excerpt": "newest",
                "distance": 0.7,
                "temporal_anchor": "1970-01-01 00:00:00",
                "temporal_confidence": 0.0,
                "valid_at": "2026-09-19 12:11:56",
                "created_at": "2026-09-19 12:11:56",
                "source_resource_id": "",
                "source_class": "",
                "verified": 0,
                "session_id": "",
                "tags": ["workflow:w"],
            }
        ]
    )
    q = _q(tags=["workflow:w"], recency_intent=True, recency_pool_weight=0.3, top_k=5)
    out = await _recency_backfill(ch, q, pool)
    sql, params = ch.query_all_calls[0]
    assert "ORDER BY coalesce(e.valid_at, e.temporal_anchor, e.created_at) DESC" in sql
    assert "hasAny(e.tags, {tags:Array(String)})" in sql and params["tags"] == ["workflow:w"]
    assert params["pooled_keys"] == ["a"] and params["newest_limit"] == 5
    assert [h.memory_key for h in out] == ["a", "w/r9/162194"]
    assert out[1].rank_score == 0.5  # the pool's floor, so recency alone decides
    assert out[1].score_components["recency_backfill"] == 1.0
    # re-ranked by the pool weight, the newest row leads
    assert [h.memory_key for h in rerank_by_recency_pool(out, weight=0.3)][0] == "w/r9/162194"
    # no hard tag → untouched, no statement
    ch2 = StubCH()
    assert (
        await _recency_backfill(ch2, _q(recency_intent=True, recency_pool_weight=0.3), pool) is pool
    )
    assert ch2.query_all_calls == []
    # soft tags, or no weight, or no cue → untouched
    assert (
        await _recency_backfill(
            ch2, _q(tags=["t"], tags_soft=True, recency_intent=True, recency_pool_weight=0.3), pool
        )
        is pool
    )
    assert (
        await _recency_backfill(
            ch2, _q(tags=["t"], recency_intent=True, recency_pool_weight=0.0), pool
        )
        is pool
    )
    assert (
        await _recency_backfill(
            ch2, _q(tags=["t"], recency_intent=False, recency_pool_weight=0.3), pool
        )
        is pool
    )
    assert ch2.query_all_calls == []


def test_geometric_decay_gives_the_newest_run_a_decisive_lead() -> None:
    # The production shape: twenty dates in the pool, rank-fusion scores
    # 0.02–0.03 apart, the newest row entering at the pool floor.
    hits = [_hit(f"r{i}", 0.03 - 0.001 * i, f"2026-08-{1 + i:02d} 00:00:00") for i in range(19)]
    hits.append(_hit("newest", 0.0, "2026-09-18 22:38:48"))
    out = rerank_by_recency_pool(hits, weight=0.3)
    assert out[0].memory_key == "newest"
    assert out[0].rank_score - out[1].rank_score > 0.1  # not a coin toss against an older row
