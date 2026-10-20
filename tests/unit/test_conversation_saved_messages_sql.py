"""Saved-message lexical ranking against the production ClickHouse statement."""

from __future__ import annotations

from typing import Any

import pytest

from server.core import bm25
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.knowledge.app.bm25 import tokenize
from server.knowledge.store.conversations import lexical_saved_messages
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(not CLICKHOUSE, reason="clickhouse binary not installed")


class _SqlClient:
    def __init__(self, insert: str) -> None:
        self.setup = [ddl("conversations"), insert]

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return run(
            self.setup,
            sql + " SETTINGS max_memory_usage=134217728, max_threads=2",
            params,
        )


async def _search(ch: Any, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return await lexical_saved_messages(
        "o", "n", "a", tokenize(query), scope=WHOLE_NAMESPACE, user_id="u", limit=limit, ch=ch
    )


@pytest.mark.parametrize("term", ["INC-48213", "events_raw123", "S3Bucket", "İSTANBUL", "ΣΟΣ.A"])
async def test_lexical_query_and_document_tokenization_agree(term: str) -> None:
    ch = _SqlClient(
        "INSERT INTO loom.conversations "
        "(org,namespace,agent,thread_id,msg_id,role,content,user_id,is_saved) "
        f"VALUES ('o','n','a','c','m','assistant',concat(repeat('filler ',400),'{term}'),'u',true)"
    )
    rows = await _search(ch, term)
    assert [r["msg_id"] for r in rows] == ["m"]
    assert term in rows[0]["content"]


async def test_lexical_bm25_uses_full_corpus_statistics_and_term_frequencies() -> None:
    ch = _SqlClient(
        "INSERT INTO loom.conversations "
        "(org,namespace,agent,thread_id,msg_id,role,content,user_id,is_saved) VALUES "
        "('o','n','a','c','one','assistant','needle needle spare','u',true),"
        "('o','n','a','c','two','assistant','needle spare spare','u',true),"
        "('o','n','a','c','none','assistant','spare spare spare','u',true)"
    )
    rows = await _search(ch, "needle")
    assert [r["msg_id"] for r in rows] == ["one", "two"]
    expected = [bm25.term(tf, bm25.idf(2, 3), dl=3, avgdl=3) for tf in (2, 1)]
    assert [r["score"] for r in rows] == pytest.approx(expected)
    both = await _search(ch, "needle spare")
    assert [r["msg_id"] for r in both] == ["one", "two", "none"]
    expected_both = [
        bm25.term(needle, bm25.idf(2, 3), dl=3, avgdl=3)
        + bm25.term(spare, bm25.idf(3, 3), dl=3, avgdl=3)
        for needle, spare in [(2, 1), (1, 2), (0, 3)]
    ]
    assert [r["score"] for r in both] == pytest.approx(expected_both)
    assert await _search(ch, "absent") == []


async def test_long_saved_messages_and_many_query_terms_fit_the_statement_memory_limit() -> None:
    ch = _SqlClient(
        "INSERT INTO loom.conversations "
        "(org,namespace,agent,thread_id,msg_id,role,content,user_id,is_saved) "
        "SELECT 'o','n','a','c',toString(number),'assistant',"
        "concat(repeat('some saved answer about deployments and events. ',2499),'needle'),"
        "'u',true FROM numbers(8)"
    )
    query = "needle " + " ".join(f"word{i}" for i in range(100))
    rows = await _search(ch, query, limit=1)
    assert len(rows) == 1
    assert rows[0]["score"] > 0
    assert rows[0]["content"].endswith("needle")
