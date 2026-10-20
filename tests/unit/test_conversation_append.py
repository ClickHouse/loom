"""conversation.append's embedding row."""

from __future__ import annotations

import pytest


def _patch_append(monkeypatch, *, append, embed):
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    async def enabled(org, ns, **k):
        return True

    async def absent(*a, **k):
        return False

    rows: list[dict] = []

    async def fake_insert_embedding(row, **k):
        rows.append(row)

    async def recheck(*a, **k):
        return None

    class _Embedder:
        model_name = "fake"
        dim = 3

        async def embed(self, text):
            return await embed(text)

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", enabled)
    monkeypatch.setattr(conversation_repo, "message_exists", absent)
    monkeypatch.setattr(conversation_repo, "append", append)
    monkeypatch.setattr(conversation_repo, "insert_embedding", fake_insert_embedding)
    monkeypatch.setattr(conversation_repo, "tombstone_embedding_if_message_gone", recheck)
    monkeypatch.setattr(conversation_repo, "saved_message_owner", recheck)
    monkeypatch.setattr(h, "get_embedder", lambda: _Embedder())
    return h, rows


def _turn(content: str = "hi"):
    from server.knowledge.api.models_conversations import ConversationAppendInput

    return ConversationAppendInput(
        org="dev",
        namespace="x",
        agent="a",
        thread_id="t",
        msg_id="m4",
        role="user",
        content=content,
    )


@pytest.mark.asyncio
async def test_the_embedding_row_keeps_the_text_its_vector_was_built_from(monkeypatch) -> None:
    """A re-embed rebuilds a conversation vector from `content_excerpt`, so the row
    holds the text append embedded, not the shorter excerpt a hit shows."""
    embedded: list[str] = []

    async def ok_append(*a, **k):
        return None

    async def embed(text):
        embedded.append(text)
        return [0.1, 0.2, 0.3]

    h, rows = _patch_append(monkeypatch, append=ok_append, embed=embed)
    content = "a" * 300 + " the deploy moved to friday " + "b" * 3000
    await h.handle_conversation_append(_turn(content))
    [row] = rows
    assert embedded == [content[:2048]]
    assert row["content_excerpt"] == content[:2048]


@pytest.mark.asyncio
async def test_a_failed_message_insert_stores_no_embedding(monkeypatch) -> None:
    """The embedding row is written only once the message row landed: a failed
    insert leaves no turn that search returns and the transcript does not hold."""

    async def failing_append(*a, **k):
        raise RuntimeError("insert failed")

    async def embed(text):
        return [0.1, 0.2, 0.3]

    h, rows = _patch_append(monkeypatch, append=failing_append, embed=embed)
    with pytest.raises(RuntimeError):
        await h.handle_conversation_append(_turn())
    assert rows == []


@pytest.mark.asyncio
async def test_a_failed_embed_stores_the_row_with_its_error(monkeypatch) -> None:
    async def ok_append(*a, **k):
        return None

    async def embed(text):
        raise RuntimeError("embedder down")

    h, rows = _patch_append(monkeypatch, append=ok_append, embed=embed)
    await h.handle_conversation_append(_turn())
    [row] = rows
    assert (row["embedding"], row["embedding_dim"], row["embed_attempts"]) == ([], 0, 0)
    assert row["embed_last_error"] == "RuntimeError: embedder down"
    assert row["embed_failed_at"] is not None
    assert (row["content_excerpt"], row["embedding_model"]) == ("hi", "fake")
