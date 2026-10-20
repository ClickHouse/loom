"""The explicit-save chat path stays independent of transcript logging."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.knowledge.api.models_conversations import (
    ConversationAppendInput,
    ConversationSaveMessageInput,
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSearchSavedMessagesInput,
)
from server.knowledge.store.conversations import StoredEmbedding, StoredMessage

_TS = datetime(2026, 1, 1)


def _save_input() -> ConversationSaveMessageInput:
    return ConversationSaveMessageInput(
        org="o",
        namespace="n",
        agent="client",
        conversation_id="c-1",
        message_id="m-1",
        role="assistant",
        content="A saved answer",
        user_id="u-1",
    )


def _stored(
    *, owners: tuple[str, ...] = ("-",), tombstoned: bool = False, unsaved: int = 1
) -> StoredMessage:
    return StoredMessage(
        role="assistant",
        content="A logged answer",
        run_id="run-7",
        ts=_TS,
        owners=owners,
        tombstoned=tombstoned,
        unsaved=unsaved,
    )


class _Store:
    """Scripted repo answers plus a record of every write the handler made."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        states: list[StoredMessage | None],
        embedding: StoredEmbedding,
    ) -> None:
        from server.knowledge.app import conversations
        from server.knowledge.store import conversations as conversation_repo

        self.states = list(states)
        self.marked: list[tuple[str, str]] = []
        self.embeddings: list[dict[str, Any]] = []
        self.appends: list[tuple[ConversationAppendInput, bool]] = []
        self.append_reason = ""

        async def message_state(*_a: object, **_kw: object) -> StoredMessage | None:
            return self.states.pop(0)

        async def embedding_state(*_a: object, **_kw: object) -> StoredEmbedding:
            return embedding

        async def mark(table: str, *_a: object, user_id: str, **_kw: object) -> None:
            self.marked.append((table, user_id))

        async def insert_embedding(row: dict[str, Any], **_kw: object) -> None:
            self.embeddings.append(row)

        async def embed_turn(content: str, **_kw: object) -> Any:
            return conversations.EmbeddedTurn(content, [0.1, 0.2, 0.3], 3, "fake", "")

        async def recheck(*_a: object, **_kw: object) -> None:
            return None

        async def append(input: ConversationAppendInput, *, saved: bool) -> SimpleNamespace:
            self.appends.append((input, saved))
            return SimpleNamespace(
                msg_id=input.msg_id, stored=True, reason=self.append_reason, ts=_TS
            )

        monkeypatch.setattr(conversation_repo, "message_state", message_state)
        monkeypatch.setattr(conversation_repo, "embedding_state", embedding_state)
        monkeypatch.setattr(conversation_repo, "mark_message_saved", mark)
        monkeypatch.setattr(conversation_repo, "insert_embedding", insert_embedding)
        monkeypatch.setattr(conversations, "_embed_turn", embed_turn)
        monkeypatch.setattr(conversation_repo, "tombstone_embedding_if_message_gone", recheck)
        monkeypatch.setattr(conversations, "_append_conversation", append)
        monkeypatch.setattr(
            conversations, "get_embedder", lambda: SimpleNamespace(model_name="fake")
        )


@pytest.mark.asyncio
async def test_save_message_bypasses_namespace_transcript_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None], StoredEmbedding(current=1, unsaved=0))
    out = await conversations.handle_conversation_save_message(_save_input())

    ((request, saved),) = store.appends
    assert saved is True
    assert request.thread_id == "c-1"
    assert request.msg_id == "m-1"
    assert request.user_id == "u-1"
    # The run id is the caller's, never a marker: saving is the is_saved flag.
    assert request.run_id == "-"
    assert out.indexed is True
    assert out.reason == ""
    assert store.marked == []


@pytest.mark.parametrize("stored_owner", ["-", "u-1"])
@pytest.mark.asyncio
async def test_save_flags_a_logged_transcript_turn_in_place(
    monkeypatch: pytest.MonkeyPatch, stored_owner: str
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [_stored(owners=(stored_owner,))], StoredEmbedding(current=1, unsaved=1)
    )
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == ""
    assert store.appends == []
    assert store.marked == [
        ("loom.conversations", "u-1"),
        ("loom.conversation_embeddings", "u-1"),
    ]


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        (_stored(owners=("u-2",)), "owner_mismatch"),
        (_stored(owners=("-", "u-2")), "owner_mismatch"),
        (_stored(owners=("u-1",), tombstoned=True), "deleted"),
        # An already-saved turn is judged the same way.
        (_stored(owners=("u-2",), unsaved=0), "owner_mismatch"),
        (_stored(owners=("u-1",), tombstoned=True, unsaved=0), "deleted"),
    ],
)
@pytest.mark.asyncio
async def test_save_refuses_a_turn_it_must_not_claim(
    monkeypatch: pytest.MonkeyPatch, stored: StoredMessage, reason: str
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [stored], StoredEmbedding(current=1, unsaved=1))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is False
    assert out.reason == reason
    assert store.marked == []
    assert store.appends == []
    assert store.embeddings == []


@pytest.mark.asyncio
async def test_resaving_a_complete_save_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [_stored(owners=("u-1",), unsaved=0)], StoredEmbedding(current=1, unsaved=0)
    )
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == "duplicate"
    assert store.marked == []
    assert store.embeddings == []


@pytest.mark.asyncio
async def test_retry_after_an_interrupted_save_writes_the_missing_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [_stored(owners=("u-1",), unsaved=0)], StoredEmbedding(current=0, unsaved=0)
    )
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == ""
    ((row,),) = [store.embeddings]
    # Embedded from the stored turn, keeping its run id.
    assert row["content_excerpt"] == "A logged answer"
    assert row["run_id"] == "run-7"
    assert row["is_saved"] is True
    assert store.marked == []


@pytest.mark.asyncio
async def test_retry_after_the_first_mutation_flags_the_embedding_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [_stored(owners=("u-1",), unsaved=0)], StoredEmbedding(current=1, unsaved=1)
    )
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == ""
    assert store.marked == [("loom.conversation_embeddings", "u-1")]


@pytest.mark.asyncio
async def test_a_turn_landing_between_read_and_append_is_judged_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [None, _stored(owners=("u-2",))], StoredEmbedding(current=1, unsaved=0)
    )
    store.append_reason = "duplicate"
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is False
    assert out.reason == "owner_mismatch"
    assert store.marked == []


@pytest.mark.asyncio
async def test_a_transcript_embedding_landing_after_a_save_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    marked: list[tuple[str, str]] = []
    inserted: list[dict[str, Any]] = []

    async def logging_on(*_a: object, **_kw: object) -> bool:
        return True

    async def not_there(*_a: object, **_kw: object) -> bool:
        return False

    async def noop(*_a: object, **_kw: object) -> None:
        return None

    async def insert_embedding(row: dict[str, Any], **_kw: object) -> None:
        inserted.append(row)

    async def saved_owner(*_a: object, **_kw: object) -> str | None:
        return "u-1"

    async def mark(table: str, *_a: object, user_id: str, **_kw: object) -> None:
        marked.append((table, user_id))

    async def embed_turn(content: str, **_kw: object) -> Any:
        return conversations.EmbeddedTurn(content, [0.1, 0.2, 0.3], 3, "fake", "")

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", logging_on)
    monkeypatch.setattr(conversation_repo, "message_exists", not_there)
    monkeypatch.setattr(conversation_repo, "append", noop)
    monkeypatch.setattr(conversation_repo, "insert_embedding", insert_embedding)
    monkeypatch.setattr(conversation_repo, "saved_message_owner", saved_owner)
    monkeypatch.setattr(conversation_repo, "mark_message_saved", mark)
    monkeypatch.setattr(conversation_repo, "tombstone_embedding_if_message_gone", noop)
    monkeypatch.setattr(conversations, "_embed_turn", embed_turn)

    await conversations.handle_conversation_append(
        ConversationAppendInput(
            org="o",
            namespace="n",
            agent="client",
            thread_id="c-1",
            msg_id="m-1",
            role="assistant",
            content="A logged answer",
        )
    )

    assert inserted[0]["is_saved"] is False
    assert marked == [("loom.conversation_embeddings", "u-1")]


@pytest.mark.asyncio
async def test_saved_message_search_has_a_user_and_selection_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    captured: dict[str, Any] = {}

    async def fake_search(
        input: ConversationSearchInput, *, saved_only: bool
    ) -> ConversationSearchOutput:
        captured["input"] = input
        captured["saved_only"] = saved_only
        return ConversationSearchOutput(hits=[])

    monkeypatch.setattr(conversations, "_search_conversations", fake_search)
    out = await conversations.handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="o", namespace="n", agent="client", user_id="u-1", query="saved answer"
        )
    )

    request = captured["input"]
    assert captured["saved_only"] is True
    assert request.user_id == "u-1"
    assert request.run_id is None
    assert request.namespace_scope == "this"
    assert out.hits == []


def test_saved_message_ops_are_registered_with_their_roles() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    save = OPERATIONS_BY_NAME["conversation.save_message"]
    search = OPERATIONS_BY_NAME["conversation.search_saved_messages"]
    assert save.required_role is Role.WRITER
    assert save.idempotent is True
    assert search.required_role is Role.VIEWER
