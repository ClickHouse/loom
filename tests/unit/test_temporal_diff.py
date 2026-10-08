"""The deterministic date-difference leg of memory.ask.

LLM readers are unreliable at date arithmetic, so the interval between two
resolved event dates is computed server-side with dateDiff and injected as
pinned evidence for the reader to report. A fake ClickHouse client routes each
query by SQL content (operand ANN lookup, then the final dateDiff). The tests
hold four rules:

  * an operand's event date is coalesce(temporal_anchor, valid_at);
  * a missing or low-similarity operand fails open to None, never a guess;
  * the injected row is pinned one ε above the highest pooled score, on the
    pool's own scale;
  * a non-temporal query injects nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from server.memory.app.retrieval import derived as ms
from server.memory.app.search.evidence import _temporal_diff_evidence_hit


class _RouteCH:
    """Routes query_all by SQL content.

    Operand ANN lookups (``cosineDistance`` over memory_embeddings) return
    the next scripted operand row; the final ``dateDiff`` query returns the
    computed integer. Operand rows are consumed in call order, which matches
    the operand_vecs order because the gather preserves it.
    """

    def __init__(self, operand_rows: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._operand_rows = list(operand_rows)

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if "dateDiff" in sql:
            a = params["a"]
            b = params["b"]
            return [{"days": (b - a).days}]
        # operand ANN lookup
        return self._operand_rows.pop(0) if self._operand_rows else []


def _operand_row(d: date, sim: float, excerpt: str = "evt") -> list[dict[str, Any]]:
    # The ANN lookup selects raw cosine distance (a bare-distance ORDER BY is
    # what keeps it eligible for CH's vector-search rewrite); similarity is
    # reconstructed by the caller, so scripted rows carry `distance`.
    return [{"event_date": d, "distance": 1.0 - sim, "excerpt": excerpt}]


# ----------------------------------------------------------------------
# fetch_temporal_diff — two-event and event-vs-observation shapes
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_event_diff_computes_interval() -> None:
    ch = _RouteCH(
        [
            _operand_row(date(2023, 2, 10), 0.62, "launched website"),
            _operand_row(date(2023, 3, 1), 0.55, "signed contract"),
        ]
    )
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is not None
    assert out["interval_days"] == 19
    # Chronologically ordered: earlier → later, magnitude only.
    assert out["from_date"] == "2023-02-10"
    assert out["to_date"] == "2023-03-01"
    assert out["to_observation"] is False


@pytest.mark.asyncio
async def test_two_event_diff_orders_pair_chronologically() -> None:
    # Operands supplied later-first; the reported interval is still the
    # non-negative magnitude (the question is direction-free).
    ch = _RouteCH(
        [
            _operand_row(date(2023, 5, 13), 0.77, "goldfinches"),
            _operand_row(date(2023, 4, 29), 0.68, "binoculars"),
        ]
    )
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is not None
    assert out["interval_days"] == 14
    assert out["from_date"] == "2023-04-29"
    assert out["to_date"] == "2023-05-13"


@pytest.mark.asyncio
async def test_event_vs_observation_diff() -> None:
    ch = _RouteCH([_operand_row(date(2022, 3, 9), 0.6, "networking event")])
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0]],
        embedding_model="m",
        observation_date="2022-04-04",
        to_observation=True,
    )
    assert out is not None
    assert out["interval_days"] == 26
    assert out["from_date"] == "2022-03-09"
    assert out["to_date"] == "2022-04-04"
    assert out["to_observation"] is True


@pytest.mark.asyncio
async def test_missing_operand_date_fails_open() -> None:
    # One operand resolves, the other returns nothing → abstain, never guess.
    ch = _RouteCH(
        [
            _operand_row(date(2023, 2, 10), 0.62),
            [],  # second operand: no dated fact
        ]
    )
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is None


@pytest.mark.asyncio
async def test_low_similarity_operand_fails_open() -> None:
    # A below-floor match is noise; binding it would inject a confidently
    # wrong interval, so the leg abstains.
    ch = _RouteCH(
        [
            _operand_row(date(2023, 2, 10), 0.62),
            _operand_row(date(2023, 3, 1), 0.10),  # below the floor
        ]
    )
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is None


@pytest.mark.asyncio
async def test_observation_diff_requires_observation_date() -> None:
    ch = _RouteCH([_operand_row(date(2022, 3, 9), 0.6)])
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0]],
        embedding_model="m",
        observation_date=None,
        to_observation=True,
    )
    assert out is None


@pytest.mark.asyncio
async def test_wrong_operand_count_abstains() -> None:
    ch = _RouteCH([_operand_row(date(2022, 3, 9), 0.6)])
    # Two-event shape with only one probe.
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is None


@pytest.mark.asyncio
async def test_operand_query_uses_coalesce_event_date() -> None:
    ch = _RouteCH(
        [
            _operand_row(date(2023, 1, 1), 0.6),
            _operand_row(date(2023, 1, 8), 0.6),
        ]
    )
    await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    operand_sql = ch.calls[0][0]
    # temporal_anchor (resolved event date) preferred over valid_at (session).
    assert "coalesce(temporal_anchor, valid_at)" in operand_sql
    # Only dated rows participate.
    assert "coalesce(temporal_anchor, valid_at) IS NOT NULL" in operand_sql


# ----------------------------------------------------------------------
# _temporal_diff_evidence_hit — pinned injection, RRF-scale invariant
# ----------------------------------------------------------------------


class _Hit:
    def __init__(self, rank_score: float) -> None:
        self.rank_score = rank_score


def test_evidence_hit_is_pinned_above_pool_max() -> None:
    pool = [_Hit(0.9), _Hit(0.5), _Hit(0.033)]
    hit = _temporal_diff_evidence_hit(
        {
            "interval_days": 14,
            "from_date": "2023-04-29",
            "to_date": "2023-05-13",
            "from_excerpt": "a",
            "to_excerpt": "b",
            "to_observation": False,
        },
        pool,
        namespace="ns",
    )
    # One ε above the highest pooled score so no later
    # rank_score sort/truncation can displace it, on the pool's own scale.
    assert hit.rank_score == pytest.approx(0.9 + 1e-6)
    assert hit.score_components["evidence_lift"] == pytest.approx(1e-6)
    assert hit.score_components["pool_ceiling"] == pytest.approx(0.9)
    assert hit.source == "computed"
    assert hit.memory_key == "temporal_diff"
    assert "[computed] interval = 14 days" in hit.content_excerpt
    assert "2023-04-29" in hit.content_excerpt
    assert "2023-05-13" in hit.content_excerpt


def test_evidence_hit_empty_pool_pins_one_epsilon_above_zero() -> None:
    hit = _temporal_diff_evidence_hit(
        {
            "interval_days": 7,
            "from_date": "2023-01-01",
            "to_date": "2023-01-08",
            "from_excerpt": "",
            "to_excerpt": "",
            "to_observation": True,
        },
        [],
        namespace="ns",
    )
    assert hit.rank_score == pytest.approx(1e-6) and hit.similarity == 1.0


def test_evidence_hit_reports_unit_conversions() -> None:
    # The line carries day count + week/month conversions so the reader
    # converts from the integer rather than re-subtracting dates.
    hit = _temporal_diff_evidence_hit(
        {
            "interval_days": 14,
            "from_date": "2023-04-29",
            "to_date": "2023-05-13",
            "from_excerpt": "",
            "to_excerpt": "",
            "to_observation": False,
        },
        [],
        namespace="ns",
    )
    assert "2.0 weeks" in hit.content_excerpt


# ----------------------------------------------------------------------
# Third-person spans bind; a single fact is not an interval;
# an event-dated row beats a mention-dated one; the reader is told which.
# ----------------------------------------------------------------------


def _keyed_row(d: date, sim: float, key: str, *, anchored: bool, excerpt: str = "evt") -> dict:
    return {
        "event_date": d,
        "distance": 1.0 - sim,
        "excerpt": excerpt,
        "memory_key": key,
        "anchored": anchored,
    }


@pytest.mark.asyncio
async def test_same_fact_for_both_operands_fails_open() -> None:
    row = _keyed_row(date(2023, 10, 4), 0.7, "dave_mustang", anchored=True, excerpt="Dave rebuilt")
    ch = _RouteCH([[row], [dict(row)]])
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is None  # "0 days" from one row is not evidence


@pytest.mark.asyncio
async def test_anchored_row_within_margin_wins_the_binding() -> None:
    mention = _keyed_row(date(2023, 8, 4), 0.60, "toby_mentioned", anchored=False)
    event = _keyed_row(date(2023, 7, 15), 0.57, "toby_adopted", anchored=True)
    far = _keyed_row(date(2023, 1, 1), 0.40, "unrelated", anchored=True)
    ch = _RouteCH([[mention, event, far]])
    res = await ms._resolve_operand_event_date(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vec=[0.0],
        embedding_model="m",
    )
    assert res is not None and res["memory_key"] == "toby_adopted" and res["anchored"] is True
    # Outside the margin the best match keeps the binding, mention-dated or not.
    ch2 = _RouteCH([[mention, _keyed_row(date(2023, 7, 15), 0.50, "toby_adopted", anchored=True)]])
    res2 = await ms._resolve_operand_event_date(
        ch2,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vec=[0.0],
        embedding_model="m",
    )
    assert res2 is not None and res2["memory_key"] == "toby_mentioned"


@pytest.mark.asyncio
async def test_mention_dated_operand_marks_the_interval_approximate() -> None:
    ch = _RouteCH(
        [
            [_keyed_row(date(2022, 7, 10), 0.6, "book_started", anchored=False, excerpt="began")],
            [_keyed_row(date(2022, 9, 29), 0.6, "book_finished", anchored=True, excerpt="done")],
        ]
    )
    out = await ms.fetch_temporal_diff(
        ch,
        org="dev",
        namespace="ns",
        agent="a",
        user_id="-",
        operand_vecs=[[0.0], [0.0]],
        embedding_model="m",
        to_observation=False,
    )
    assert out is not None and out["interval_days"] == 81 and out["all_event_dated"] is False
    assert out["from_event_dated"] is False and out["to_event_dated"] is True
    hit = _temporal_diff_evidence_hit(out, [], namespace="ns")
    assert "AT LEAST 81 days" in hit.content_excerpt
    upper = _temporal_diff_evidence_hit(
        {**out, "from_event_dated": True, "to_event_dated": False}, [], namespace="ns"
    )
    assert "AT MOST 81 days" in upper.content_excerpt
    exact = _temporal_diff_evidence_hit(
        {**out, "from_event_dated": True, "to_event_dated": True}, [], namespace="ns"
    )
    assert "AT LEAST" not in exact.content_excerpt and "AT MOST" not in exact.content_excerpt
