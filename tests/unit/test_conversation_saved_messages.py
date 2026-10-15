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
    *,
    owners: tuple[str, ...] = ("-",),
    tombstoned: bool = False,
    unsaved: int = 1,
    role: str = "assistant",
) -> StoredMessage:
    return StoredMessage(
        role=role,
        content="A logged answer",
        run_id="run-7",
        ts=_TS,
        owners=owners,
        tombstoned=tombstoned,
        unsaved=unsaved,
    )


_MINE = _stored(owners=("u-1",), unsaved=0)


def _embedding(current: int = 1, unsaved: int = 0, foreign: int = 0) -> StoredEmbedding:
    return StoredEmbedding(current=current, unsaved=unsaved, foreign=foreign)


class _Store:
    """Scripted repo answers plus a record of every write the handler made.

    `states` answers message_state in order, the last answer repeating."""

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
        self.appends: list[tuple[Any, bool]] = []
        self.embed_error = ""

        async def message_state(*_a: object, **_kw: object) -> StoredMessage | None:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

        async def embedding_state(*_a: object, **_kw: object) -> StoredEmbedding:
            return embedding

        async def mark(table: str, *_a: object, user_id: str, **_kw: object) -> None:
            self.marked.append((table, user_id))

        async def insert_embedding(row: dict[str, Any], **_kw: object) -> None:
            self.embeddings.append(row)

        async def embed_turn(content: str, **_kw: object) -> Any:
            if self.embed_error:
                return conversations.EmbeddedTurn(content, [], 0, "fake", self.embed_error)
            return conversations.EmbeddedTurn(content, [0.1, 0.2, 0.3], 3, "fake", "")

        async def recheck(*_a: object, **_kw: object) -> None:
            return None

        async def append(msg: Any, *, once_per_identity: bool = False, **_kw: object) -> None:
            self.appends.append((msg, once_per_identity))

        monkeypatch.setattr(conversation_repo, "message_state", message_state)
        monkeypatch.setattr(conversation_repo, "embedding_state", embedding_state)
        monkeypatch.setattr(conversation_repo, "mark_message_saved", mark)
        monkeypatch.setattr(conversation_repo, "insert_embedding", insert_embedding)
        monkeypatch.setattr(conversation_repo, "tombstone_embedding_if_message_gone", recheck)
        monkeypatch.setattr(conversation_repo, "append", append)
        monkeypatch.setattr(conversations, "_embed_turn", embed_turn)
        monkeypatch.setattr(
            conversations, "get_embedder", lambda: SimpleNamespace(model_name="fake")
        )


@pytest.mark.asyncio
async def test_save_message_bypasses_namespace_transcript_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new turn is written saved, with no read of the logging switch (the store
    stub has none to answer)."""
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None, _MINE], _embedding(current=0))
    out = await conversations.handle_conversation_save_message(_save_input())

    ((msg, once_per_identity),) = store.appends
    assert once_per_identity is True
    assert (msg.thread_id, msg.msg_id, msg.user_id, msg.is_saved) == ("c-1", "m-1", "u-1", True)
    # The run id is the caller's, never a marker: saving is the is_saved flag.
    assert msg.run_id == "-"
    ((row,),) = [store.embeddings]
    assert (row["is_saved"], row["user_id"]) == (True, "u-1")
    assert out.indexed is True
    assert out.reason == ""
    assert store.marked == []


@pytest.mark.asyncio
async def test_a_save_whose_block_was_dropped_writes_the_turn_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block of an erased turn still in the deduplication window drops the
    identity-keyed write; the re-read finds nothing and the turn is written plainly."""
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None, None, _MINE], _embedding(current=0))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert [once for _, once in store.appends] == [True, False]
    assert out.indexed is True


@pytest.mark.parametrize("stored_owner", ["-", "u-1"])
@pytest.mark.asyncio
async def test_save_flags_a_logged_transcript_turn_in_place(
    monkeypatch: pytest.MonkeyPatch, stored_owner: str
) -> None:
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch, [_stored(owners=(stored_owner,)), _MINE], _embedding(current=1, unsaved=1)
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
        (_stored(role="system"), "unsupported_role"),
        (_stored(role="tool"), "unsupported_role"),
        # An already-saved turn is judged the same way.
        (_stored(owners=("u-2",), unsaved=0), "owner_mismatch"),
        (_stored(owners=("u-1",), tombstoned=True, unsaved=0), "deleted"),
        (_stored(owners=("u-1",), unsaved=0, role="system"), "unsupported_role"),
    ],
)
@pytest.mark.asyncio
async def test_save_refuses_a_turn_it_must_not_claim(
    monkeypatch: pytest.MonkeyPatch, stored: StoredMessage, reason: str
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [stored], _embedding(current=1, unsaved=1))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is False
    assert out.reason == reason
    assert store.marked == []
    assert store.appends == []
    assert store.embeddings == []


@pytest.mark.asyncio
async def test_a_save_that_loses_the_claim_leaves_the_embedding_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another user's save claimed the unowned turn first: the claim matched
    nothing, the re-read shows their ownership, and the embedding is not touched."""
    from server.knowledge.app import conversations

    store = _Store(
        monkeypatch,
        [_stored(owners=("-",)), _stored(owners=("u-2",), unsaved=1)],
        _embedding(current=1, unsaved=1),
    )
    out = await conversations.handle_conversation_save_message(_save_input())

    assert (out.indexed, out.reason) == (False, "owner_mismatch")
    assert store.marked == [("loom.conversations", "u-1")]
    assert store.embeddings == []


@pytest.mark.asyncio
async def test_an_embedding_another_user_owns_is_not_taken_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [_MINE], _embedding(current=1, unsaved=1, foreign=1))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert (out.indexed, out.reason) == (False, "owner_mismatch")
    assert store.marked == []
    assert store.embeddings == []


@pytest.mark.asyncio
async def test_resaving_a_complete_save_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [_MINE], _embedding(current=1))
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

    store = _Store(monkeypatch, [_MINE], _embedding(current=0))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == ""
    ((row,),) = [store.embeddings]
    # Embedded from the stored turn, keeping its run id and ts.
    assert row["content_excerpt"] == "A logged answer"
    assert (row["run_id"], row["ts"]) == ("run-7", _TS)
    assert row["is_saved"] is True
    assert store.marked == []


@pytest.mark.asyncio
async def test_a_failed_embed_is_reported_and_retried_on_the_next_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save during an embedder outage stores the turn saved with a placeholder
    and says so; `embedding_state` counts no placeholder as a vector, so saving
    again embeds the turn."""
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None, _MINE], _embedding(current=0))
    store.embed_error = "RuntimeError: embedder down"
    out = await conversations.handle_conversation_save_message(_save_input())

    assert (out.indexed, out.reason) == (False, "embed_failed")
    [placeholder] = store.embeddings
    assert (placeholder["embedding_dim"], placeholder["is_saved"]) == (0, True)

    store.embed_error = ""
    again = await conversations.handle_conversation_save_message(_save_input())
    assert (again.indexed, again.reason) == (True, "")
    assert store.embeddings[-1]["embedding_dim"] == 3


@pytest.mark.asyncio
async def test_retry_after_the_first_mutation_flags_the_embedding_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [_MINE], _embedding(current=1, unsaved=1))
    out = await conversations.handle_conversation_save_message(_save_input())

    assert out.indexed is True
    assert out.reason == ""
    assert store.marked == [("loom.conversation_embeddings", "u-1")]


@pytest.mark.asyncio
async def test_a_turn_another_writer_landed_first_is_judged_on_its_stored_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None, _stored(owners=("u-2",))], _embedding(current=1))
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


@pytest.mark.asyncio
async def test_a_session_bounded_token_cannot_search_saved_messages() -> None:
    """Conversation rows carry no session_id: an own_session scope has nothing to
    filter on, so the search is refused rather than widened to the user."""
    from server.core.errors import AuthInsufficientError
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound
    from server.knowledge.app import conversations

    scope = ReadFilters(scope="own_session", user_id="u-1", session_id="s-1")
    with read_filters_bound(None, scope), pytest.raises(AuthInsufficientError):
        await conversations.handle_conversation_search_saved_messages(
            ConversationSearchSavedMessagesInput(
                org="o", namespace="n", agent="librechat", user_id="u-1", query="q"
            )
        )
