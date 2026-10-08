"""Shared dummies for the corpus cases: identities, vectors, row factories."""

from __future__ import annotations

from typing import Any

ORG = "o"
NS = "n"
AGENT = "bot"
MODEL = "text-embedding-3-small"
DIM = 1536
VEC = [0.1] * DIM
SMALL_VEC = [0.1] * 8


def rows_by_substring(table: dict[str, list[dict[str, Any]]]):
    """A `rows(sql, params)` function answering the first matching key."""

    def _rows(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        for needle, rows in table.items():
            if needle in sql:
                return rows
        return []

    return _rows


def fact_hit(key: str = "deploy_window", **extra: Any) -> dict[str, Any]:
    row = {
        "memory_key": key,
        "memory_type": "preference",
        "content_excerpt": "Deploy Tue/Thu, never Friday",
        "distance": 0.21,
        "rank_score": 0.79,
        "similarity": 0.79,
        "org": ORG,
        "namespace": NS,
        "scope": "private",
        "embedding_model": MODEL,
        "agent": AGENT,
        "user_id": "-",
        "session_id": "s1",
        "value_distance": 0.1,
        "tags": [],
        "entities": [],
        "confidence": 0.9,
        "version": 1,
        "created_at": None,
    }
    row.update(extra)
    return row


def chunk_cand(session: str = "s1", index: int = 3, distance: float = 0.1) -> dict[str, Any]:
    return {
        "session_id": session,
        "chunk_index": index,
        "distance": distance,
        "parent_memory_key": f"p_{session}",
    }
