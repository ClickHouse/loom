"""Unit tests for the session-summary RETRIEVAL leg.

The write side + builder live in test_session_summaries.py. These tests stub the CH
client and assert the leg's hit shape, scope predicate, and fail-open behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import SearchHit, fetch_session_summaries
from server.memory.app.retrieval.summaries import (
    _HELD_MARGIN,
    _SESSION_SUMMARY_OVERFETCH,
    _SESSION_SUMMARY_TOP_N,
    dedup_summary_rows,
)


class StubCH:
    def __init__(self, returns: list[dict[str, Any]] | None = None, raise_on_query: bool = False):
        self.returns = returns or []
        self.raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if self.raise_on_query:
            raise RuntimeError("cold table")
        return self.returns


@pytest.mark.asyncio
async def test_returns_summary_source_hits_with_label() -> None:
    ch = StubCH(
        returns=[
            {
                "session_id": "sess-1",
                "content_excerpt": "On 2024-03-11 the user moved to Berlin.",
                "distance": 0.18,
                "valid_at_iso": "2024-03-11 00:00:00",
            }
        ]
    )
    hits = await fetch_session_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u1",
        query_vec=[0.1] * 8,
        embedding_model="text-embedding-3-small",
    )
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, SearchHit)
    assert h.source == "summary"
    assert h.memory_key == "summary:sess-1"
    assert h.valid_at == "2024-03-11 00:00:00"  # label parsed through, not a sort key
    assert abs(h.rank_score - 0.82) < 1e-9
    assert abs((1.0 - h.distance) - 0.82) < 1e-9


@pytest.mark.asyncio
async def test_label_sentinel_reads_undated() -> None:
    ch = StubCH(
        returns=[
            {
                "session_id": "s2",
                "content_excerpt": "digest",
                "distance": 0.5,
                "valid_at_iso": "1970-01-01 00:00:00",
            }
        ]
    )
    hits = await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="bot", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    assert hits[0].valid_at == ""  # coalesce sentinel surfaces as undated


@pytest.mark.asyncio
async def test_fail_open_empty_on_ch_error() -> None:
    ch = StubCH(raise_on_query=True)
    hits = await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="bot", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    assert hits == []


@pytest.mark.asyncio
async def test_agent_wildcard_drops_agent_predicate() -> None:
    ch = StubCH(returns=[])
    await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="*", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    sql, params = ch.calls[0]
    assert "agent = {agent:String}" not in sql
    assert "agent" not in params


@pytest.mark.asyncio
async def test_explicit_agent_filters_on_agent() -> None:
    ch = StubCH(returns=[])
    await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="bot", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    sql, params = ch.calls[0]
    assert "agent = {agent:String}" in sql
    assert params["agent"] == "bot"


@pytest.mark.asyncio
async def test_default_n_overfetches_and_dedups_in_python() -> None:
    """The statement orders by the bare distance and carries
    no `LIMIT 1 BY` — the codes-first shortlist and the lazy column read
    need that shape — so it over-fetches and the per-session dedup and the
    (distance, session_id) order happen in Python."""
    ch = StubCH(returns=[])
    await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="bot", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    sql, params = ch.calls[0]
    # An explicit agent is one row per session under FINAL: N rows, plus the spare rows
    # for a digest held back for reading as a credential.
    assert params["lim"] == _SESSION_SUMMARY_TOP_N + _HELD_MARGIN
    assert "n" not in params
    # The wildcard can fold several agents' digests of one session: over-fetch.
    ch_star = StubCH(returns=[])
    await fetch_session_summaries(
        ch_star,
        org="o",
        namespace="ns",
        agent="*",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
    )
    assert (
        ch_star.calls[0][1]["lim"]
        == _SESSION_SUMMARY_TOP_N * _SESSION_SUMMARY_OVERFETCH + _HELD_MARGIN
    )
    code = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    assert "LIMIT 1 BY" not in code
    assert "ORDER BY distance ASC\n" in code and "session_id ASC" not in code
    assert "tombstoned_at IS NULL" in sql
    assert "embedding_dim > 0" in sql


def test_dedup_keeps_the_closest_row_per_session_in_served_order() -> None:
    rows = [
        {"session_id": "s2", "distance": 0.30},
        {"session_id": "s1", "distance": 0.20},
        {"session_id": "s2", "distance": 0.25},  # a second agent's digest of s2
        {"session_id": "s3", "distance": 0.25},  # ties break on session_id
        {"session_id": "s4", "distance": 0.40},
    ]
    out = dedup_summary_rows(rows, n=3)
    assert [(r["session_id"], r["distance"]) for r in out] == [
        ("s1", 0.20),
        ("s2", 0.25),
        ("s3", 0.25),
    ]
    assert dedup_summary_rows([], n=3) == []


@pytest.mark.asyncio
async def test_hits_are_deduped_per_session_and_capped_at_n() -> None:
    ch = StubCH(
        returns=[
            {"session_id": "a", "distance": 0.1, "content_excerpt": "x", "valid_at_iso": ""},
            {"session_id": "a", "distance": 0.2, "content_excerpt": "y", "valid_at_iso": ""},
            {"session_id": "b", "distance": 0.3, "content_excerpt": "z", "valid_at_iso": ""},
        ]
    )
    hits = await fetch_session_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="*",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        n=1,
    )
    assert [h.memory_key for h in hits] == ["summary:a"]
    assert hits[0].content_excerpt == "x"


@pytest.mark.asyncio
async def test_a_session_bound_read_keeps_to_its_sessions() -> None:
    ch = StubCH(returns=[])
    await fetch_session_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        session_id="s1",
        session_ids=["s1", "s2"],
    )
    sql, params = ch.calls[0]
    assert "session_id = {session_id:String}" in sql and params["session_id"] == "s1"
    assert "session_id IN {session_ids:Array(String)}" in sql
    assert params["session_ids"] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_no_session_bound_reads_every_session() -> None:
    ch = StubCH(returns=[])
    await fetch_session_summaries(
        ch, org="o", namespace="ns", agent="bot", user_id="u", query_vec=[0.1], embedding_model="m"
    )
    sql, params = ch.calls[0]
    assert "session_id =" not in sql and "session_ids" not in params
