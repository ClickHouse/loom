"""An unchecked source never makes a session digest eligible for retrieval."""

from __future__ import annotations

from typing import Any

from server.memory.app.retrieval.summaries import fetch_session_summaries


async def test_source_state_failure_withholds_the_summary() -> None:
    class UnavailableSources:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "FROM loom.memory FINAL" in sql:
                raise TimeoutError("source state unavailable")
            return [
                {
                    "agent": "writer",
                    "user_id": "reader",
                    "session_id": "session",
                    "version": 200,
                    "derived_from": ["source"],
                    "summary_text": "old fact",
                    "distance": 0.1,
                }
            ]

    hits = await fetch_session_summaries(
        UnavailableSources(),
        org="org",
        namespace="namespace",
        agent="writer",
        user_id="reader",
        query_vec=[1.0, 0.0],
        embedding_model="test-model",
    )
    assert hits == []
