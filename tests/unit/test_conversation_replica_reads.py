"""Saved-state point reads see committed writes across ClickHouse replicas."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.ch import query_settings, sequential_reads_active
from server.knowledge.api.models_conversations import (
    ConversationAppendInput,
    ConversationSaveMessageInput,
    ConversationUnsaveMessageInput,
)
from server.knowledge.app import conversations as app
from server.knowledge.store import conversations as repo


class _Replicas:
    def __init__(self) -> None:
        self.transcript: list[dict[str, Any]] = []
        self.embeddings: list[dict[str, Any]] = []

    async def insert(self, table, rows, settings=None):
        target = self.transcript if table == "loom.conversations" else self.embeddings
        target.extend(rows)

    async def query_one(self, sql, params=None):
        visible = bool(query_settings().get("select_sequential_consistency"))
        messages = self.transcript if visible else []
        vectors = self.embeddings[-1:] if visible else []
        if "SELECT 1 AS ok" in sql:
            return {"ok": 1} if messages else None
        if "groupUniqArray(user_id)" in sql:
            if not messages:
                return {"n": 0}
            row = messages[-1]
            return {
                "n": len(messages),
                "role": row["role"],
                "content": row["content"],
                "run_id": row["run_id"],
                "last_ts": row["ts"],
                "last_saved_at": row["saved_at"],
                "owners": [row["user_id"]],
                "tombstoned": False,
                "unsaved": int(not row["is_saved"]),
            }
        if "AS current" in sql:
            return {
                "current": len(vectors),
                "unsaved": sum(not row["is_saved"] for row in vectors),
                "foreign": 0,
                "version_ts": vectors[-1]["ts"] if vectors else None,
            }
        if "max(tombstoned_at) AS gone" in sql:
            return {"n": len(messages), "gone": None}
        if "SELECT user_id FROM" in sql:
            rows = vectors if "loom.conversation_embeddings" in sql else messages
            return next(({"user_id": row["user_id"]} for row in rows if row["is_saved"]), None)
        raise AssertionError(sql)

    async def execute(self, sql, params=None):
        assert "UPDATE is_saved = false" in sql
        rows = self.embeddings if "loom.conversation_embeddings" in sql else self.transcript
        for row in rows:
            row.update(is_saved=False, saved_at=None)

    def saved_turn(self) -> None:
        when = datetime(2025, 1, 1, tzinfo=UTC)
        self.transcript.append(
            {
                "org": "o",
                "namespace": "n",
                "agent": "a",
                "thread_id": "t",
                "msg_id": "m",
                "role": "assistant",
                "content": "hello",
                "user_id": "u",
                "run_id": "-",
                "is_saved": True,
                "saved_at": when,
                "ts": when,
            }
        )
        self.embeddings.append(dict(self.transcript[0], content_excerpt="hello"))


@pytest.fixture(autouse=True)
def _saved_write_leases(monkeypatch):
    from tests.unit.claims_fake import held_leases

    held_leases(monkeypatch)


@pytest.mark.parametrize("operation", ["append", "save", "save_existing", "unsave"])
async def test_saved_state_reads_see_the_write_on_a_lagging_replica(monkeypatch, operation):
    async def logging_on(*args, **kwargs):
        return True

    async def embed(content, **kwargs):
        return app.EmbeddedTurn(content, [0.1], 1, "fake", "")

    ch = _Replicas()
    if operation in ("save_existing", "unsave"):
        ch.saved_turn()
    monkeypatch.setattr(repo, "_default_client", lambda: ch)
    monkeypatch.setattr(repo, "namespace_logging_enabled", logging_on)
    monkeypatch.setattr(app, "_embed_turn", embed)
    monkeypatch.setattr(app, "get_embedder", lambda: SimpleNamespace(model_name="fake"))
    identity = dict(org="o", namespace="n", agent="a", user_id="u")
    if operation == "append":
        out = await app.handle_conversation_append(
            ConversationAppendInput(
                **identity,
                thread_id="t",
                msg_id="m",
                role="assistant",
                content="hello",
            )
        )
        assert out.stored and out.reason == ""
    elif operation == "unsave":
        out = await app.handle_conversation_unsave_message(
            ConversationUnsaveMessageInput(
                **identity,
                conversation_id="t",
                message_id="m",
            )
        )
        assert out.found and not out.removed
    else:
        out = await app.handle_conversation_save_message(
            ConversationSaveMessageInput(
                **identity,
                conversation_id="t",
                message_id="m",
                role="assistant",
                content="hello",
            )
        )
        assert out.indexed
        assert out.reason == ("duplicate" if operation == "save_existing" else "")
    expected_saved = operation in ("save", "save_existing")
    assert all(row["is_saved"] == expected_saved for row in ch.transcript + ch.embeddings)
    assert len(ch.transcript) == len(ch.embeddings) == 1
    assert ch.embeddings[0]["msg_id"] == ch.transcript[0]["msg_id"] == "m"
    assert ch.embeddings[0]["content_excerpt"] == ch.transcript[0]["content"] == "hello"
    assert not sequential_reads_active()
