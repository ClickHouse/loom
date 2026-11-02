"""The lexical fallback, the unmatched-candidate leg and the recency backfill, run on
clickhouse-local against the real `loom.memory_embeddings`: rows that tie on their order
come back in row order."""

from __future__ import annotations

import pytest

from server.memory.app.retrieval.pool import _recency_backfill
from server.memory.app.retrieval.side_legs import (
    _fetch_unmatched_candidates,
    _lexical_recall_fallback,
)
from tests.unit.clickhouse_local import CLICKHOUSE, VECTOR, LocalCH, ddl
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(session: str) -> str:
    # one fixed time for every row, so they tie on freshness and recency whatever the clock;
    # each row is its own insert, and read without a tiebreak they come back out of order
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding,"
        " embedding_model, embedding_dim, version, tags, valid_at, created_at,"
        " last_reinforced_at) VALUES ('clickhouse', 'ns', 'bot', 'u',"
        f" '{session}', 'k', 'semantic', 'private', 'one value', {VECTOR}, {VECTOR},"
        " 'text-embedding-3-small', 3072, 1, ['kind:run'], toDateTime(0), toDateTime(0),"
        " toDateTime(0))"
    )


def _local_ch(*rows: str) -> LocalCH:
    return LocalCH("SET enable_quantized_codec = 1", ddl("memory_embeddings"), *rows)


@pytest.mark.asyncio
async def test_tied_unmatched_rows_come_back_in_row_order() -> None:
    # one value in four sessions ties on every signal the leg orders by
    ch = _local_ch(*(_row(s) for s in ("s4", "s2", "s3", "s1")))
    q = _q(query_vec=[0.1] * 3072, user_id="u", agent="bot")
    hits = await _fetch_unmatched_candidates(ch, q, [], limit=3)
    assert [h.session_id for h in hits] == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_tied_newest_rows_join_the_pool_in_row_order() -> None:
    ch = _local_ch(*(_row(s) for s in ("s4", "s2", "s3", "s1")))
    q = _q(
        query_vec=[0.1] * 3072,
        user_id="u",
        agent="bot",
        tags=["kind:run"],
        recency_intent=True,
        recency_pool_weight=0.5,
        top_k=3,
    )
    hits = await _recency_backfill(ch, q, [])
    assert [h.session_id for h in hits] == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_tied_lexical_rows_come_back_in_row_order() -> None:
    ch = _local_ch(*(_row(s) for s in ("s4", "s2", "s3", "s1")))
    q = _q(query_vec=[0.1] * 3072, user_id="u", agent="bot", query_text="one value")
    hits = await _lexical_recall_fallback(ch, q, limit=3)
    assert [h.session_id for h in hits] == ["s1", "s2", "s3"]
