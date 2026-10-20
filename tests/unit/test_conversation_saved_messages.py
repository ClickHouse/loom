"""The explicit-save chat path stays independent of transcript logging."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.knowledge.api.models_conversations import (
    ConversationAppendInput,
    ConversationListSavedMessagesInput,
    ConversationSavedStatusInput,
    ConversationSaveMessageInput,
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSearchSavedMessagesInput,
    ConversationUnsaveMessageInput,
)
from server.knowledge.store.conversations import StoredEmbedding, StoredMessage
from tests.unit._fake_credentials import OPENAI_KEY


@pytest.fixture(autouse=True)
def _saved_write_leases(monkeypatch):
    from tests.unit.claims_fake import held_leases

    held_leases(monkeypatch)


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

    written = False

    async def exists(*_a: object, **_kw: object) -> bool:
        return written

    async def record(*_a: object, **_kw: object) -> None:
        nonlocal written
        written = True

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
    monkeypatch.setattr(conversation_repo, "message_exists", exists)
    monkeypatch.setattr(conversation_repo, "append", record)
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

    async def fake_lexical(*args: Any, **kw: Any) -> list[dict[str, Any]]:
        captured["lexical"] = (args, kw)
        return []

    from server.knowledge.store import conversations as conversation_repo

    monkeypatch.setattr(conversations, "_search_conversations", fake_search)
    monkeypatch.setattr(conversation_repo, "lexical_saved_messages", fake_lexical)
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
    args, kw = captured["lexical"]
    assert args[:4] == ("o", "n", "client", ["saved", "answer"])
    assert kw["user_id"] == "u-1"
    assert kw["scope"].narrowed is False
    assert out.hits == []


def _hit(mid: str, excerpt: str = "x") -> Any:
    from server.knowledge.api.models_conversations import _SavedMessageHit

    return _SavedMessageHit(
        conversation_id="c",
        message_id=mid,
        role="user",
        content_excerpt=excerpt,
        ts=datetime(2026, 1, 1),
    )


def test_fusion_rewards_a_message_both_legs_found_and_keeps_the_lexical_excerpt() -> None:
    from server.knowledge.app.conversations import _fuse_saved

    fused = _fuse_saved(
        [_hit("a"), _hit("b", "opening"), _hit("c")],
        [_hit("b", "…the matched words…"), _hit("d")],
        top_k=3,
        w_lex=1.0,
    )
    assert [h.message_id for h in fused] == ["b", "a", "d"]
    assert fused[0].content_excerpt == "…the matched words…"


def test_fusion_with_no_lexical_weight_keeps_the_semantic_order() -> None:
    from server.knowledge.app.conversations import _fuse_saved

    fused = _fuse_saved([_hit("a"), _hit("b")], [_hit("b"), _hit("z")], top_k=3, w_lex=0.0)
    assert [h.message_id for h in fused][:2] == ["a", "b"]


def test_fusion_ties_keep_the_semantic_order() -> None:
    from server.knowledge.app.conversations import _fuse_saved

    fused = _fuse_saved([_hit("a"), _hit("b")], [_hit("b"), _hit("a")], top_k=2, w_lex=1.0)
    assert [h.message_id for h in fused] == ["a", "b"]


@pytest.mark.parametrize("top_k", [1, 200])
async def test_hybrid_search_ranks_agreement_below_each_legs_first_hit(
    monkeypatch: pytest.MonkeyPatch,
    top_k: int,
) -> None:
    from server.knowledge.api.models_conversations import _ConversationHit
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    async def semantic(request: Any, *, saved_only: bool) -> ConversationSearchOutput:
        return ConversationSearchOutput(
            hits=[
                _ConversationHit(
                    thread_id="c",
                    msg_id=mid,
                    role="user",
                    content_excerpt=mid,
                    ts=datetime(2026, 1, 1),
                )
                for mid in [*[f"semantic-{i}" for i in range(top_k)], "agreement"][: request.top_k]
            ]
        )

    async def lexical(*args: Any, **kw: Any) -> list[dict[str, Any]]:
        return [
            dict(thread_id="c", msg_id=mid, role="user", content=mid, saved_at=datetime(2026, 1, 1))
            for mid in [*[f"lexical-{i}" for i in range(top_k)], "agreement"][: kw["limit"]]
        ]

    monkeypatch.setattr(conversations, "_search_conversations", semantic)
    monkeypatch.setattr(conversation_repo, "lexical_saved_messages", lexical)
    out = await conversations.handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            query="term",
            top_k=top_k,
        )
    )
    assert "agreement" in [h.message_id for h in out.hits]
    assert len(out.hits) == top_k


def test_snippet_centres_a_deep_match_and_keeps_an_early_one_at_the_start() -> None:
    from server.knowledge.app.conversations import _snippet

    deep = "filler " * 400 + "ticket INC-48213 closed"
    out = _snippet(deep, ["inc"])
    assert out.startswith("…") and "INC-48213" in out
    early = "INC-48213 opens the message " + "filler " * 100
    assert _snippet(early, ["inc"]).startswith("INC-48213")


def test_the_saved_message_cuts_follow_a_live_excerpt_length(monkeypatch) -> None:
    """A saved listing's excerpt and a lexical hit's snippet read
    conversation_search_excerpt_chars per call."""
    from server.knowledge.app.conversations import _excerpt, _snippet
    from server.settings import settings

    monkeypatch.setattr(settings, "conversation_search_excerpt_chars", 12)
    assert _excerpt("x" * 50) == "x" * 12 + "…"
    window = _snippet("filler " * 400 + "ticket INC-48213 closed", ["inc"])
    assert window.startswith("…") and len(window.strip("…")) == 12


def test_saved_message_ops_are_registered_with_their_roles() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    save = OPERATIONS_BY_NAME["conversation.save_message"]
    search = OPERATIONS_BY_NAME["conversation.search_saved_messages"]
    assert save.required_role is Role.WRITER
    assert save.idempotent is True
    assert search.required_role is Role.VIEWER


@pytest.mark.asyncio
async def test_a_new_turn_that_reads_as_a_credential_is_refused_and_not_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations

    store = _Store(monkeypatch, [None], _embedding(current=0))
    out = await conversations.handle_conversation_save_message(
        _save_input().model_copy(update={"content": f"my key is {OPENAI_KEY}"})
    )
    assert (out.indexed, out.reason) == (False, "reads_as_credential")
    assert store.appends == [] and store.embeddings == [] and store.marked == []


@pytest.mark.asyncio
async def test_a_stored_turn_that_reads_as_a_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored text wins over the request's: a logged turn holding a key is never
    embedded, so the saved search could never find it."""
    from server.knowledge.app import conversations

    stored = StoredMessage(
        role="assistant",
        content=f"use {OPENAI_KEY}",
        run_id="run-7",
        ts=_TS,
        owners=("-",),
        tombstoned=False,
        unsaved=1,
    )
    store = _Store(monkeypatch, [stored], _embedding(current=0))
    out = await conversations.handle_conversation_save_message(_save_input())
    assert (out.indexed, out.reason) == (False, "reads_as_credential")
    assert store.marked == [] and store.embeddings == []


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


def _unsave_input(user_id: str = "u-1") -> ConversationUnsaveMessageInput:
    return ConversationUnsaveMessageInput(
        org="o",
        namespace="n",
        agent="librechat",
        conversation_id="c-1",
        message_id="m-1",
        user_id=user_id,
    )


def _stub_unsave(
    monkeypatch: pytest.MonkeyPatch, *, owner: str | None, recording: bool
) -> list[dict[str, object]]:
    from server.knowledge.store import conversations as conversation_repo

    calls: list[dict[str, object]] = []

    async def fake_owner(*_a: object, **_kw: object) -> str | None:
        return owner

    async def fake_logging(*_a: object, **_kw: object) -> bool:
        return recording

    async def fake_unsave(table: str, *_a: object, **kw: object) -> None:
        calls.append({"table": table, **kw})

    monkeypatch.setattr(conversation_repo, "saved_message_owner", fake_owner)
    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", fake_logging)
    monkeypatch.setattr(conversation_repo, "unsave_message", fake_unsave)
    return calls


@pytest.mark.parametrize("owner", [None, "u-2"])
@pytest.mark.asyncio
async def test_unsave_reports_not_found_for_a_message_this_user_never_saved(
    monkeypatch: pytest.MonkeyPatch, owner: str | None
) -> None:
    """Another user's saved message reads as not found, never as theirs."""
    from server.knowledge.app import conversations

    calls = _stub_unsave(monkeypatch, owner=owner, recording=False)
    out = await conversations.handle_conversation_unsave_message(_unsave_input())

    assert out.found is False
    assert out.removed is False
    assert calls == []


@pytest.mark.parametrize(("recording", "removed"), [(False, True), (True, False)])
@pytest.mark.asyncio
async def test_unsave_removes_the_turn_only_where_transcripts_are_not_recorded(
    monkeypatch: pytest.MonkeyPatch, recording: bool, removed: bool
) -> None:
    from server.knowledge.app import conversations

    calls = _stub_unsave(monkeypatch, owner="u-1", recording=recording)
    out = await conversations.handle_conversation_unsave_message(_unsave_input())

    assert out.found is True
    assert out.removed is removed
    assert {c["table"] for c in calls} == {"loom.conversations", "loom.conversation_embeddings"}
    assert all(c["remove"] is removed and c["user_id"] == "u-1" for c in calls)


def test_unsave_is_registered_as_an_idempotent_write() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["conversation.unsave_message"]
    assert op.required_role is Role.WRITER
    assert op.idempotent is True


@pytest.mark.asyncio
async def test_the_saved_list_cursor_carries_the_conversation_and_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message id is unique only within its conversation, so the page key
    names both; the next page starts strictly after the last row."""
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    seen: list[object] = []
    at = datetime(2026, 1, 1, 12, 0, 0)

    async def fake_list(*_a: object, **kw: object) -> list[dict[str, object]]:
        seen.append(kw["before"])
        return [
            {"thread_id": "c-1", "msg_id": "m-2", "role": "user", "content": "b", "saved_at": at},
            {"thread_id": "c-1", "msg_id": "m-1", "role": "user", "content": "a", "saved_at": at},
        ]

    monkeypatch.setattr(conversation_repo, "list_saved_messages", fake_list)
    first = await conversations.handle_conversation_list_saved_messages(
        ConversationListSavedMessagesInput(
            org="o", namespace="n", agent="librechat", user_id="u-1", limit=1
        )
    )
    assert [r.message_id for r in first.rows] == ["m-2"]
    assert first.next_cursor is not None

    await conversations.handle_conversation_list_saved_messages(
        ConversationListSavedMessagesInput(
            org="o", namespace="n", agent="librechat", user_id="u-1", cursor=first.next_cursor
        )
    )
    assert seen == [None, (at, "c-1", "m-2")]


@pytest.mark.asyncio
async def test_the_saved_list_serializes_only_the_fields_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    at = datetime(2026, 1, 1, 12, 0, 0)

    async def fake_list(*_a: object, **_kw: object) -> list[dict[str, object]]:
        return [
            {"thread_id": "c-1", "msg_id": "m-1", "role": "user", "content": "a", "saved_at": at}
        ]

    monkeypatch.setattr(conversation_repo, "list_saved_messages", fake_list)
    out = await conversations.handle_conversation_list_saved_messages(
        ConversationListSavedMessagesInput(
            org="o", namespace="n", agent="client", user_id="u-1", fields=["role"]
        )
    )
    assert out.model_dump()["rows"] == [
        {"conversation_id": "c-1", "message_id": "m-1", "role": "user"}
    ]


@pytest.mark.asyncio
async def test_saved_status_answers_in_the_order_asked_without_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    async def fake_ids(*_a: object, **_kw: object) -> set[str]:
        return {"m-1", "m-3"}

    monkeypatch.setattr(conversation_repo, "saved_message_ids", fake_ids)
    out = await conversations.handle_conversation_saved_status(
        ConversationSavedStatusInput(
            org="o",
            namespace="n",
            agent="librechat",
            user_id="u-1",
            conversation_id="c-1",
            message_ids=["m-3", "m-2", "m-1", "m-3"],
        )
    )
    assert out.saved == ["m-3", "m-1"]


@pytest.mark.asyncio
async def test_the_keyword_leg_holds_back_a_saved_message_that_reads_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message an earlier build saved with a key in it is never a keyword hit; the leg
    asks for spare rows so the page is not left short."""
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo
    from server.memory.domain.sensitivity import HELD_MARGIN

    at = datetime(2026, 1, 1, 12, 0, 0)
    asked: list[int] = []

    def row(mid: str, content: str) -> dict[str, Any]:
        return {
            "thread_id": "c-1",
            "msg_id": mid,
            "role": "user",
            "content": content,
            "saved_at": at,
        }

    async def fake_lexical(*_a: Any, limit: int, **_kw: Any) -> list[dict[str, Any]]:
        asked.append(limit)
        return [
            row("m-key", f"deploy key {OPENAI_KEY}"),
            row("m-1", "deploy notes"),
            row("m-2", "deploy"),
        ]

    monkeypatch.setattr(conversation_repo, "lexical_saved_messages", fake_lexical)
    out = await conversations.handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="o",
            namespace="n",
            agent="client",
            user_id="u-1",
            query="deploy",
            search_mode="lexical",
            top_k=2,
        )
    )
    assert [h.message_id for h in out.hits] == ["m-1", "m-2"]
    assert asked == [2 + HELD_MARGIN]


@pytest.mark.asyncio
async def test_the_saved_list_shows_no_text_of_a_message_that_reads_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    at = datetime(2026, 1, 1, 12, 0, 0)

    async def fake_list(*_a: object, **_kw: object) -> list[dict[str, object]]:
        return [
            {
                "thread_id": "c-1",
                "msg_id": "m-k",
                "role": "user",
                "content": OPENAI_KEY,
                "saved_at": at,
            },
            {"thread_id": "c-1", "msg_id": "m-1", "role": "user", "content": "a", "saved_at": at},
        ]

    monkeypatch.setattr(conversation_repo, "list_saved_messages", fake_list)
    out = await conversations.handle_conversation_list_saved_messages(
        ConversationListSavedMessagesInput(org="o", namespace="n", agent="client", user_id="u-1")
    )
    assert [(r.message_id, r.content_excerpt) for r in out.rows] == [("m-k", ""), ("m-1", "a")]
