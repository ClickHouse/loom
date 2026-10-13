"""The query spelling lookup against ClickHouse: it reads only the rows the
search may read, at their latest version, and samples whole-word matches."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from server.memory.domain.search_types import SearchQuery
from server.memory.store.query_spellings import SAMPLE_ROWS, spelling_counts

MODEL = "fake-casing-test"
# The stored vector column is quantized at Loom's standard width.
VEC = [0.1] * 3072


class _Async:
    """The async `query_all` the store calls, over the sync test client."""

    def __init__(self, client: Any) -> None:
        self._c = client

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self._c.query(sql, parameters=params).named_results())


@pytest.fixture
def ns(ch_client: Any) -> Any:
    name = f"test-casing-{uuid.uuid4().hex[:10]}"
    yield name
    ch_client.command(
        "DELETE FROM loom.memory_embeddings WHERE org = 'test' AND namespace = {ns:String}",
        parameters={"ns": name},
    )


def _insert(ch: Any, ns: str, rows: list[tuple[str, str, str, str, int]], **extra: Any) -> None:
    """rows: (memory_key, content, user_id, session_id, version); `extra` sets a column on
    every row. Both vector columns are quantized, so both carry a full-width vector."""
    cols = {
        "org": "test",
        "namespace": ns,
        "agent": "a",
        "memory_type": "semantic",
        "scope": "private",
        "embedding_model": MODEL,
        "embedding_dim": len(VEC),
        "embedding": VEC,
        "memory_key_embedding_dim": len(VEC),
        "memory_key_embedding": VEC,
        **extra,
    }
    names = ["memory_key", "content_excerpt", "user_id", "session_id", "version", *cols]
    ch.insert(
        "loom.memory_embeddings",
        [[*row, *cols.values()] for row in rows],
        column_names=names,
    )


def _q(ns: str, **kw: Any) -> SearchQuery:
    return SearchQuery(
        org="test",
        namespace=ns,
        scope="private",
        agent="*",
        embedding_model=MODEL,
        query_vec=[],
        top_k=0,
        **kw,
    )


def _counts(ch: Any, q: SearchQuery, tokens: list[str]) -> dict[str, int]:
    return asyncio.run(spelling_counts(_Async(ch), q, tokens))


def test_a_user_pinned_read_never_sees_another_users_spelling(ch_client, ns):
    _insert(ch_client, ns, [("k2", "DataLakeCatalog bug", "u2", "s2", 1)])
    assert _counts(ch_client, _q(ns, user_id="u1"), ["datalakecatalog"]) == {}
    assert _counts(ch_client, _q(ns, user_id="u2"), ["datalakecatalog"]) == {"DataLakeCatalog": 1}


def test_a_session_pinned_read_never_sees_another_sessions_spelling(ch_client, ns):
    _insert(ch_client, ns, [("k2", "DataLakeCatalog bug", "u1", "s2", 1)])
    assert _counts(ch_client, _q(ns, session_id="s1"), ["datalakecatalog"]) == {}
    assert _counts(ch_client, _q(ns, session_ids=["s1"]), ["datalakecatalog"]) == {}
    assert _counts(ch_client, _q(ns, session_id="s2"), ["datalakecatalog"]) == {
        "DataLakeCatalog": 1
    }


def test_only_the_latest_version_votes(ch_client, ns):
    # Two inserts, so the old versions sit in their own part until a merge.
    _insert(ch_client, ns, [("k1", "DataLakeCatalog", "u", "s", 1)])
    _insert(ch_client, ns, [("k1", "datalakecatalog", "u", "s", 2)])
    assert _counts(ch_client, _q(ns), ["datalakecatalog"]) == {"datalakecatalog": 1}


def test_a_deleted_row_does_not_vote(ch_client, ns):
    _insert(ch_client, ns, [("k1", "DataLakeCatalog", "u", "s", 1)])
    _insert(ch_client, ns, [("k1", "DataLakeCatalog", "u", "s", 2)], status="tombstoned")
    assert _counts(ch_client, _q(ns), ["datalakecatalog"]) == {}


def test_longer_words_never_fill_the_sample(ch_client, ns):
    rows = [
        (f"k{i}", "datalakecatalogue datalakecatalog2 x_datalakecatalog", "u", "s", 1)
        for i in range(SAMPLE_ROWS)
    ]
    _insert(ch_client, ns, [*rows, ("real", "the DataLakeCatalog engine", "u", "s", 1)])
    assert _counts(ch_client, _q(ns), ["datalakecatalog"]) == {"DataLakeCatalog": 1}
