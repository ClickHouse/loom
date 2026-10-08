"""The typed-triple matcher and the graph arm's flat triple boost.

CH-free: the matcher against a stub client and the graph-arm SQL.
"""

from __future__ import annotations

from typing import Any

import pytest


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return [{"memory_key": "k1", "matches": 2}, {"memory_key": "k2", "matches": 2}]


@pytest.mark.asyncio
async def test_matcher_returns_keys_in_one_read() -> None:
    from server.memory.app.retrieval import fetch_triple_matched_memories

    ch = _CH()
    keys = await fetch_triple_matched_memories(
        ch,
        org="o",
        namespace="n",
        query_triples=[("alice", "works_at", "acme")],
    )
    assert keys == ["k1", "k2"] and len(ch.calls) == 1


@pytest.mark.asyncio
async def test_graph_arm_boosts_a_matched_key_flat() -> None:
    from server.memory.app.retrieval import vector_search
    from server.memory.domain.search_types import SearchQuery

    class _SQL:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self.params: list[dict[str, Any]] = []

        async def query_all(self, sql, params=None):
            self.sql.append(sql)
            self.params.append(params or {})
            return []

    base = dict(
        org="o",
        namespace="n",
        agent="a",
        scope="private",
        embedding_model="fake",
        query_vec=[0.1] * 4,
        top_k=5,
        search_mode="graph",
        triple_matched_keys=["k1", "k2"],
    )
    ch = _SQL()
    await vector_search(SearchQuery(**base), ch=ch)
    main = [s for s in ch.sql if "FROM loom.memory_embeddings AS e" in s][0]
    assert "if(has({triple_keys:Array(String)}, memory_key)" in main


@pytest.mark.asyncio
async def test_matcher_statement_is_one_parseable_line() -> None:
    """A `-- comment` spliced into the one-line UNION would swallow the
    closing parenthesis; ClickHouse would answer "Unmatched parentheses" and
    the fail-open leg would return nothing on every graph-mode query."""
    from server.memory.app.retrieval import fetch_triple_matched_memories

    ch = _CH()
    await fetch_triple_matched_memories(
        ch,
        org="o",
        namespace="n",
        query_triples=[("alice", "works_at", "acme"), ("?", "lives_in", "paris")],
    )
    sql, _ = ch.calls[0]
    assert "--" not in sql
    assert sql.count("(") == sql.count(")")
    assert sql.count("SELECT tm.memory_key") == 2 and " UNION ALL " in sql
    assert sql.rstrip().endswith("LIMIT 200") or "LIMIT" in sql
