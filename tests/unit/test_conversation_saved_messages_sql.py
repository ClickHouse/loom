"""Saved-message ranking and retirement against production ClickHouse statements."""

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


class _MutationClient:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: dict[str, Any] = {}

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)
        self.params.update(params or {})

    async def query_all(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


@pytest.mark.parametrize("resave", [False, True])
async def test_unsave_does_not_reveal_an_older_embedding_version(resave: bool) -> None:
    from datetime import UTC, datetime

    from server.knowledge.store.conversations import unsave_message

    ch = _MutationClient()
    await unsave_message(
        "loom.conversation_embeddings",
        "o",
        "n",
        "b",
        "c",
        "m",
        user_id="user-b",
        remove=True,
        when=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
        ch=ch,
    )
    columns = "org,namespace,agent,thread_id,msg_id,role,content_excerpt,embedding_model,embedding_dim,embedding,user_id,is_saved,ts"

    def version(agent: str, saved: bool, second: int) -> str:
        return (
            f"INSERT INTO loom.conversation_embeddings ({columns}) SELECT "
            f"'o','n','{agent}','c','m','assistant','shared','model',3072,"
            f"arrayResize([toFloat32(1)],3072),'user-{agent}',{str(saved).lower()},"
            f"toDateTime64('2025-01-01 00:00:0{second}',3,'UTC')"
        )

    setup = [
        "SET enable_quantized_codec=1",
        ddl("conversations"),
        ddl("conversation_embeddings"),
        "ALTER TABLE loom.conversation_embeddings MODIFY SETTING "
        "max_bytes_to_merge_at_max_space_in_pool=1, max_bytes_to_merge_at_min_space_in_pool=1",
        "INSERT INTO loom.conversations "
        "(org,namespace,agent,thread_id,msg_id,role,content,user_id,is_saved) "
        "VALUES ('o','n','b','c','m','assistant','shared','user-b',true)",
        version("a", False, 1),
        version("b", True, 2),
        *ch.statements,
    ]
    if resave:
        setup.append(version("b", True, 3))
    rows = run(
        setup,
        "SELECT agent, user_id, is_saved FROM loom.conversation_embeddings FINAL "
        "WHERE tombstoned_at IS NULL",
        ch.params,
    )
    assert rows == ([{"agent": "b", "user_id": "user-b", "is_saved": True}] if resave else [])
