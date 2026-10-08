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
        self.inserted: list[dict[str, Any]] = []
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sql.append(sql)
        # ClickHouse answers FINAL on a plain MergeTree with code 181.
        assert "loom.conversations FINAL" not in sql, "FINAL on plain MergeTree"
        return self.rows

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        self.inserted.extend(rows)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **kw: Any) -> None:
        self.sql.append(sql)


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


def test_fitting_prefix_keeps_at_least_one_line() -> None:
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 21) == 2
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 20) == 1
    assert ch_mod._fitting_prefix(["a" * 50], 20) == 1
