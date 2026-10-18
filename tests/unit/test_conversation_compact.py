"""conversation.compact reads a plain MergeTree (FINAL is illegal there, code
181) and writes a summary row that erasure-by-user must be able to find."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from server.knowledge.api.models_conversations import ConversationCompactInput
from server.knowledge.app import conversations as ch_mod
from server.llm.client import LLMResult

T0 = datetime(2026, 9, 1, 12, 0, 0)


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.inserted: list[dict[str, Any]] = []  # loom.conversations rows
        self.embedded: list[dict[str, Any]] = []  # loom.conversation_embeddings rows
        self.sql: list[str] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sql.append(sql)
        # ClickHouse answers FINAL on a plain MergeTree with code 181.
        assert "loom.conversations FINAL" not in sql, "FINAL on plain MergeTree"
        if "FROM loom.conversation_embeddings" in sql:
            # Every compacted turn's embedding is live and held by no other agent.
            return [{"msg_id": m} for m in (params or {}).get("ids", [])]
        return self.rows

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        return None  # the shared embedding cache reads it as a miss

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        (self.embedded if table == "loom.conversation_embeddings" else self.inserted).extend(rows)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **kw: Any) -> None:
        self.sql.append(sql)
        self.executed.append((sql, params or {}))


_EMBEDDING_FIELDS = (
    "org",
    "namespace",
    "agent",
    "thread_id",
    "msg_id",
    "role",
    "content_excerpt",
    "user_id",
    "run_id",
    "ts",
    "embed_attempts",
    "embed_last_error",
    "embed_failed_at",
)


class _FakeLLM:
    provider = "fake"
    model = "fake"

    async def complete(self, **kw: Any) -> LLMResult:
        return LLMResult(text="a summary", tokens_in=1, tokens_out=2, provider="fake", model="fake")


def test_compact_reads_without_final_and_carries_the_threads_user(monkeypatch) -> None:
    import server.core.ch
    import server.llm.client

    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(8)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=2, min_to_compact=2
            )
        )
    )
    assert out.compacted is True
    [summary] = fake.inserted
    assert summary["user_id"] == "alice"
    assert summary["run_id"] == "run-1"
    assert summary["ts"] == T0
    # The summary is embedded, so search finds what it replaced.
    [vec] = fake.embedded
    assert {k: vec[k] for k in _EMBEDDING_FIELDS} == {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "thread_id": "t",
        "msg_id": summary["msg_id"],
        "role": "system",
        "content_excerpt": "a summary",
        "user_id": "alice",  # erasure-by-user finds the summary's vector too
        "run_id": "run-1",
        "ts": T0,
        "embed_attempts": 1,
        "embed_last_error": "",
        "embed_failed_at": None,
    }
    assert vec["embedding_dim"] > 0 and len(vec["embedding"]) == vec["embedding_dim"]
    assert vec["embedding_model"] == ch_mod.get_embedder().model_name


def test_compact_tombstones_only_the_messages_the_summary_read(monkeypatch) -> None:
    """The window is cut where the prompt budget ends: a message the summarizer never
    saw stays verbatim for the next run instead of being tombstoned."""
    import server.core.ch
    import server.llm.client

    long = "x" * 7000
    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user",
            "content": long,
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(6)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    # Five candidates of ~7 KB each; two fit the 16,000-character prompt.
    assert out.compacted is True and out.messages_summarized == 2
    [summary] = fake.inserted
    assert '"compacted_from": ["m0", "m1"]' in summary["metadata"]
    # The compacted turns leave conversation.search too: their embeddings are
    # tombstoned with the messages, by the same ids.
    tombstoned = {
        table: params.get("ids") or params.get("due")
        for sql, params in fake.executed
        for table in ("loom.conversations", "loom.conversation_embeddings")
        if f"ALTER TABLE {table}\n" in sql
    }
    assert tombstoned == {
        "loom.conversations": ["m0", "m1"],
        "loom.conversation_embeddings": ["m0", "m1"],
    }
    # Embeddings first, so a retry after a failed second mutation repeats both.
    first = next(sql for sql, _ in fake.executed if "ALTER TABLE" in sql)
    assert "loom.conversation_embeddings" in first


def test_fitting_prefix_keeps_at_least_one_line() -> None:
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 21) == 2
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 20) == 1
    assert ch_mod._fitting_prefix(["a" * 50], 20) == 1


def test_a_long_thread_is_tombstoned_in_chunks() -> None:
    """Each mutation names at most `_TOMBSTONE_CHUNK` turns, so every statement's id
    list stays bounded however long the thread."""
    from server.knowledge.store import conversations as repo

    due = [f"m{i}" for i in range(2 * repo._TOMBSTONE_CHUNK + 5)]

    class _CH(_FakeCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            return [{"msg_id": m} for m in due]

    fake = _CH([])
    asyncio.run(repo._tombstone_embeddings("o", "n", "a", "t", None, when=T0, ch=fake))
    chunks = [p["due"] for sql, p in fake.executed if "conversation_embeddings" in sql]
    assert [len(c) for c in chunks] == [repo._TOMBSTONE_CHUNK, repo._TOMBSTONE_CHUNK, 5]
    assert sum(chunks, []) == due


def test_a_summary_that_cannot_be_embedded_compacts_nothing(monkeypatch) -> None:
    """Without the summary's vector the compacted turns would leave search for good,
    so nothing is written and the originals stay as they are."""
    import server.core.ch
    import server.llm.client

    class _DownEmbedder:
        model_name = "fake"
        dim = 3

        async def embed(self, text):
            raise RuntimeError("embedder down")

    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(6)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(ch_mod, "get_embedder", lambda: _DownEmbedder())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "embed-failed")
    assert fake.inserted == [] and fake.embedded == []
    assert not [sql for sql, _ in fake.executed if "ALTER TABLE" in sql]
