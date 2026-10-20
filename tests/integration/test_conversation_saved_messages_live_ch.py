"""conversation.save_message against live ClickHouse: an interrupted or racing
write is repaired, so the saved-message search still finds the message.

Skips cleanly when LOOM_CH_HOST is unset. Each test writes under its own
namespace.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Literal

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

_CONTENT = "the deploy window moved to thursday afternoon"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def namespace() -> str:
    return f"itest-saved-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _logging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript path needs the namespace's logging switch on; the test
    namespace is never registered, so the switch would read off."""
    from server.knowledge.store import conversations as conversation_repo

    async def on(*_a: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", on)


def _save(namespace: str, user_id: str = "u-1") -> Any:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput

    return ConversationSaveMessageInput(
        org="itest-org",
        namespace=namespace,
        agent="itest-client",
        conversation_id="c-1",
        message_id="m-1",
        role="assistant",
        content=_CONTENT,
        user_id=user_id,
    )


def _transcript_turn(namespace: str) -> Any:
    from server.knowledge.api.models_conversations import ConversationAppendInput

    return ConversationAppendInput(
        org="itest-org",
        namespace=namespace,
        agent="itest-client",
        thread_id="c-1",
        msg_id="m-1",
        role="assistant",
        content=_CONTENT,
        run_id="run-transcript",
    )


async def _saved_hits(
    namespace: str,
    user_id: str = "u-1",
    search_mode: Literal["hybrid", "semantic", "lexical"] = "hybrid",
) -> list[Any]:
    from server.knowledge.api.models_conversations import ConversationSearchSavedMessagesInput
    from server.knowledge.app.conversations import handle_conversation_search_saved_messages

    out = await handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            user_id=user_id,
            query=_CONTENT,
            search_mode=search_mode,
        )
    )
    return out.hits


async def _rows(ch: Any, table: str, namespace: str) -> list[dict[str, Any]]:
    return await ch.query_all(
        f"SELECT run_id, user_id, is_saved FROM {table} "
        "WHERE org = 'itest-org' AND namespace = {ns:String} AND msg_id = 'm-1'",
        params={"ns": namespace},
    )


@pytest.mark.asyncio
async def test_retry_repairs_a_save_interrupted_after_its_first_mutation(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app.conversations import (
        handle_conversation_append,
        handle_conversation_save_message,
    )
    from server.knowledge.store import conversations as conversation_repo

    await handle_conversation_append(_transcript_turn(namespace))

    real_mark = conversation_repo.mark_message_saved

    async def fail_on_embeddings(table: str, *a: Any, **kw: Any) -> None:
        if table == "loom.conversation_embeddings":
            raise RuntimeError("connection dropped")
        await real_mark(table, *a, **kw)

    monkeypatch.setattr(conversation_repo, "mark_message_saved", fail_on_embeddings)
    with pytest.raises(RuntimeError):
        await handle_conversation_save_message(_save(namespace))
    # The message row is flagged, its embedding row is not: the vector leg
    # misses it until the retry repairs the embedding row.
    assert await _saved_hits(namespace, search_mode="semantic") == []

    monkeypatch.setattr(conversation_repo, "mark_message_saved", real_mark)
    out = await handle_conversation_save_message(_save(namespace))

    assert out.indexed is True
    assert out.reason == ""
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]
    # Saving flags the rows; the transcript's run id stays as recorded.
    for table in ("loom.conversations", "loom.conversation_embeddings"):
        rows = await _rows(ch, table, namespace)
        assert rows and all(r["is_saved"] and r["user_id"] == "u-1" for r in rows)
        assert {r["run_id"] for r in rows} == {"run-transcript"}

    again = await handle_conversation_save_message(_save(namespace))
    assert again.reason == "duplicate"


@pytest.mark.asyncio
async def test_retry_writes_the_embedding_a_new_save_never_landed(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app.conversations import handle_conversation_save_message
    from server.knowledge.store import conversations as conversation_repo

    real_insert = conversation_repo.insert_embedding

    async def dropped(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(conversation_repo, "insert_embedding", dropped)
    with pytest.raises(RuntimeError):
        await handle_conversation_save_message(_save(namespace))

    monkeypatch.setattr(conversation_repo, "insert_embedding", real_insert)
    out = await handle_conversation_save_message(_save(namespace))

    assert out.indexed is True
    assert out.reason == ""
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]


@pytest.mark.parametrize("save_clock_behind", [False, True])
@pytest.mark.asyncio
async def test_a_save_while_the_transcript_turn_is_embedding_stays_searchable(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch, save_clock_behind: bool
) -> None:
    """The save lands between the transcript's message insert and its embedding
    insert, and writes its own saved embedding row. When the save's clock is
    behind the transcript's (another replica), the transcript's unflagged row
    has the later ts and replaces the saved one under FINAL, unless the append
    flags it."""
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo

    real_append = conversation_repo.append
    saves: list[Any] = []

    async def append_then_save(msg: Any, **kw: Any) -> None:
        await real_append(msg, **kw)
        if not msg.is_saved:
            if save_clock_behind:
                with monkeypatch.context() as m:
                    m.setattr(
                        conversations,
                        "naive_utc_now",
                        lambda: naive_utc_now() - timedelta(minutes=1),
                    )
                    saves.append(
                        await conversations.handle_conversation_save_message(_save(namespace))
                    )
            else:
                saves.append(await conversations.handle_conversation_save_message(_save(namespace)))

    monkeypatch.setattr(conversation_repo, "append", append_then_save)
    await conversations.handle_conversation_append(_transcript_turn(namespace))

    assert [s.indexed for s in saves] == [True]
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]
    rows = await ch.query_all(
        "SELECT is_saved, user_id FROM loom.conversation_embeddings FINAL "
        "WHERE org = 'itest-org' AND namespace = {ns:String} AND msg_id = 'm-1'",
        params={"ns": namespace},
    )
    assert rows and all(r["is_saved"] and r["user_id"] == "u-1" for r in rows)


@pytest.mark.asyncio
async def test_an_already_saved_message_is_refused_to_another_user(ch: Any, namespace: str) -> None:
    from server.knowledge.app.conversations import handle_conversation_save_message

    first = await handle_conversation_save_message(_save(namespace))
    other = await handle_conversation_save_message(_save(namespace, user_id="u-2"))

    assert first.indexed is True
    assert other.indexed is False
    assert other.reason == "owner_mismatch"
    assert await _saved_hits(namespace, user_id="u-2") == []


@pytest.mark.asyncio
async def test_two_concurrent_saves_of_a_new_message_store_one_transcript_row(
    ch: Any, namespace: str
) -> None:
    """Both saves read no row and both write one; the identity token lands it once."""
    import asyncio

    from server.knowledge.app.conversations import handle_conversation_save_message

    outs = await asyncio.gather(
        handle_conversation_save_message(_save(namespace)),
        handle_conversation_save_message(_save(namespace)),
    )

    assert all(o.indexed for o in outs)
    assert len(await _rows(ch, "loom.conversations", namespace)) == 1
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]


@pytest.mark.asyncio
async def test_two_users_claiming_one_logged_turn_leave_one_owner_on_both_tables(
    ch: Any, namespace: str
) -> None:
    import asyncio

    from server.knowledge.app.conversations import (
        handle_conversation_append,
        handle_conversation_save_message,
    )

    await handle_conversation_append(_transcript_turn(namespace))
    outs = await asyncio.gather(
        handle_conversation_save_message(_save(namespace, user_id="u-1")),
        handle_conversation_save_message(_save(namespace, user_id="u-2")),
    )

    [winner] = [u for u, o in zip(("u-1", "u-2"), outs, strict=True) if o.indexed]
    [loser] = [o for o in outs if not o.indexed]
    assert loser.reason == "owner_mismatch"
    for table in ("loom.conversations", "loom.conversation_embeddings"):
        assert {r["user_id"] for r in await _rows(ch, table, namespace)} == {winner}


@pytest.mark.asyncio
async def test_a_save_during_an_embedder_outage_is_embedded_by_the_retry(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.embedding import get_embedder
    from server.knowledge.app.conversations import handle_conversation_save_message

    async def down(_text: str) -> list[float]:
        raise RuntimeError("embedder down")

    with monkeypatch.context() as m:
        m.setattr(get_embedder(), "embed", down)
        out = await handle_conversation_save_message(_save(namespace))
    assert (out.indexed, out.reason) == (False, "embed_failed")
    # The BM25 leg reads the saved text, so the turn is found before it has a vector.
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]
    dims = await ch.query_all(
        "SELECT embedding_dim FROM loom.conversation_embeddings FINAL "
        "WHERE org = 'itest-org' AND namespace = {ns:String} AND msg_id = 'm-1'",
        params={"ns": namespace},
    )
    assert [r["embedding_dim"] for r in dims] == [0]

    again = await handle_conversation_save_message(_save(namespace))
    assert (again.indexed, again.reason) == (True, "")
    assert [h.message_id for h in await _saved_hits(namespace)] == ["m-1"]


@pytest.mark.asyncio
async def test_a_stored_system_turn_is_refused_and_left_unsaved(ch: Any, namespace: str) -> None:
    from server.knowledge.app.conversations import (
        handle_conversation_append,
        handle_conversation_save_message,
    )

    await handle_conversation_append(
        _transcript_turn(namespace).model_copy(update={"role": "system"})
    )
    out = await handle_conversation_save_message(_save(namespace))

    assert (out.indexed, out.reason) == (False, "unsupported_role")
    assert [r["is_saved"] for r in await _rows(ch, "loom.conversations", namespace)] == [False]
