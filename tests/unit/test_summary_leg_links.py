"""The summary leg's rows carry their links, session and run scope into the
hit."""

from __future__ import annotations

from typing import Any


def test_digest_row_carries_links_and_session_into_the_hit() -> None:
    """The summary leg's row → SearchHit mapping (memory.app.retrieval)."""
    import asyncio

    from server.memory.app.retrieval import fetch_session_summaries

    class _CH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            assert "derived_from" in sql
            return [
                {
                    "session_id": "s1",
                    "summary_text": "On 2023-05-08 the user …",
                    "distance": 0.3,
                    "valid_at_iso": "2023-05-08 00:00:00",
                    "derived_from": ["caroline_trip", "caroline_hobby"],
                }
            ]

    hits = asyncio.run(
        fetch_session_summaries(
            _CH(),
            org="o",
            namespace="ns",
            agent="*",
            user_id=None,
            query_vec=[0.1],
            embedding_model="m",
        )
    )
    assert len(hits) == 1 and hits[0].source == "summary"
    assert hits[0].session_id == "s1" and hits[0].derived_from == [
        "caroline_trip",
        "caroline_hobby",
    ]


def test_digest_leg_keeps_to_the_run_when_scoped() -> None:
    """A run_id on the search reaches the summary leg as the
    same equality predicate the fact and chunk legs carry; unscoped reads
    add none, so a run-scoped search never returns another run's digest."""
    import asyncio

    from server.memory.app.retrieval import fetch_session_summaries

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append((sql, dict(params or {})))
            return []

    ch = _CH()
    asyncio.run(
        fetch_session_summaries(
            ch,
            org="o",
            namespace="ns",
            agent="*",
            user_id=None,
            query_vec=[0.1],
            embedding_model="m",
            run_id="run-7",
        )
    )
    sql, params = ch.calls[0]
    assert "run_id = {run_id:String}" in sql and params["run_id"] == "run-7"

    ch = _CH()
    asyncio.run(
        fetch_session_summaries(
            ch,
            org="o",
            namespace="ns",
            agent="*",
            user_id=None,
            query_vec=[0.1],
            embedding_model="m",
        )
    )
    sql, params = ch.calls[0]
    assert "run_id" not in sql and "run_id" not in params
