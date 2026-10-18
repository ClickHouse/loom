"""conversation.append's embedding row."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_the_embedding_row_keeps_the_text_its_vector_was_built_from(monkeypatch) -> None:
    """A re-embed rebuilds a conversation vector from `content_excerpt`, so the row
    holds the text append embedded, not the shorter excerpt a hit shows."""
    from server.knowledge.api.models_conversations import ConversationAppendInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    async def enabled(org, ns, **k):
        return True

    async def absent(*a, **k):
        return False

    async def fake_append(*a, **k):
        return None

    rows: list[dict] = []

    async def fake_insert_embedding(row, **k):
        rows.append(row)

    class _Embedder:
        model_name = "fake"
        dim = 3

        def __init__(self) -> None:
            self.embedded: list[str] = []

        async def embed(self, text):
            self.embedded.append(text)
            return [0.1, 0.2, 0.3]

    embedder = _Embedder()
    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", enabled)
    monkeypatch.setattr(conversation_repo, "message_exists", absent)
    monkeypatch.setattr(conversation_repo, "append", fake_append)
    monkeypatch.setattr(conversation_repo, "insert_embedding", fake_insert_embedding)
    monkeypatch.setattr(h, "get_embedder", lambda: embedder)

    content = "a" * 300 + " the deploy moved to friday " + "b" * 3000
    await h.handle_conversation_append(
        ConversationAppendInput(
            org="dev",
            namespace="x",
            agent="a",
            thread_id="t",
            msg_id="m3",
            role="user",
            content=content,
        )
    )
    [row] = rows
    assert embedder.embedded == [content[:2048]]
    assert row["content_excerpt"] == content[:2048]
