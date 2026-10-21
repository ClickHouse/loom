"""conversation.unsave_message against live ClickHouse: the message leaves the
saved search, a recorded transcript keeps its turn, an unrecorded one drops
it, another user cannot withdraw it, and erasure by user removes saved rows.

Skips cleanly when LOOM_CH_HOST is unset. Each test writes under its own
namespace (and, for erasure, its own org).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

_CONTENT = "the migration window is the second sunday of the month"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def namespace() -> str:
    return f"itest-unsave-{uuid.uuid4().hex[:12]}"


def _recording(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """The test namespace is never registered, so pin its logging switch."""
    from server.knowledge.store import conversations as conversation_repo

    async def switch(*_a: object, **_kw: object) -> bool:
        return on

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", switch)


async def _save(org: str, namespace: str, user_id: str = "u-1", *, content: str = _CONTENT) -> None:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_save_message

    out = await handle_conversation_save_message(
        ConversationSaveMessageInput(
            org=org,
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-1",
            message_id="m-1",
            role="assistant",
            content=content,
            user_id=user_id,
        )
    )
    assert out.indexed is True, out


async def _unsave(org: str, namespace: str, user_id: str = "u-1") -> Any:
    from server.knowledge.api.models_conversations import ConversationUnsaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_unsave_message

    return await handle_conversation_unsave_message(
        ConversationUnsaveMessageInput(
            org=org,
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-1",
            message_id="m-1",
            user_id=user_id,
        )
    )


async def _saved_ids(org: str, namespace: str, user_id: str = "u-1") -> list[str]:
    from server.knowledge.api.models_conversations import ConversationSearchSavedMessagesInput
    from server.knowledge.app.conversations import handle_conversation_search_saved_messages

    out = await handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org=org, namespace=namespace, agent="itest-client", user_id=user_id, query=_CONTENT
        )
    )
    return [h.message_id for h in out.hits]


async def _live_rows(ch: Any, org: str, namespace: str, table: str) -> list[dict[str, Any]]:
    return await ch.query_all(
        f"SELECT is_saved, user_id FROM {table} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND msg_id = 'm-1' "
        "AND tombstoned_at IS NULL",
        params={"org": org, "ns": namespace},
    )


@pytest.mark.asyncio
async def test_unsave_in_an_unrecorded_namespace_drops_the_turn_and_a_resave_restores_it(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording(monkeypatch, False)
    await _save("itest-org", namespace)
    assert await _saved_ids("itest-org", namespace) == ["m-1"]

    out = await _unsave("itest-org", namespace)
    assert (out.found, out.removed) == (True, True)
    assert await _saved_ids("itest-org", namespace) == []
    for table in ("loom.conversations", "loom.conversation_embeddings"):
        assert await _live_rows(ch, "itest-org", namespace, table) == []

    again = await _unsave("itest-org", namespace)
    assert again.found is False


@pytest.mark.asyncio
async def test_unsave_in_a_recorded_namespace_keeps_the_transcript_turn(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.api.models_conversations import ConversationAppendInput
    from server.knowledge.app.conversations import handle_conversation_append

    _recording(monkeypatch, True)
    await handle_conversation_append(
        ConversationAppendInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            thread_id="c-1",
            msg_id="m-1",
            role="assistant",
            content=_CONTENT,
            user_id="u-1",
        )
    )
    await _save("itest-org", namespace)

    out = await _unsave("itest-org", namespace)
    assert (out.found, out.removed) == (True, False)
    assert await _saved_ids("itest-org", namespace) == []
    rows = await _live_rows(ch, "itest-org", namespace, "loom.conversations")
    assert rows and not any(r["is_saved"] for r in rows)


@pytest.mark.asyncio
async def test_another_user_cannot_withdraw_a_saved_message(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording(monkeypatch, False)
    await _save("itest-org", namespace, user_id="u-1")

    out = await _unsave("itest-org", namespace, user_id="u-2")
    assert out.found is False
    assert await _saved_ids("itest-org", namespace) == ["m-1"]


@pytest.mark.asyncio
async def test_erasure_by_user_removes_their_saved_messages(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.erasure.store.steps_ledgers import tombstone_conversations_by_user

    org = f"itest-unsave-erase-{uuid.uuid4().hex[:8]}"
    _recording(monkeypatch, False)
    await _save(org, namespace, user_id="u-erase")
    assert await _saved_ids(org, namespace, user_id="u-erase") == ["m-1"]

    await tombstone_conversations_by_user(org, "u-erase")

    assert await _saved_ids(org, namespace, user_id="u-erase") == []
    for table in ("loom.conversations", "loom.conversation_embeddings"):
        assert await _live_rows(ch, org, namespace, table) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize(
    ("content", "legacy_excerpt"),
    [(_CONTENT, False), ((_CONTENT * 6)[:240], False), ((_CONTENT * 10)[:507], True)],
    ids=["short", "exactly-240", "legacy-excerpt"],
)
async def test_unsave_keeps_the_shared_vector_for_another_agents_live_copy(
    ch: Any,
    namespace: str,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    legacy_excerpt: bool,
    interrupted: bool,
) -> None:
    from server.core.datetimes import naive_utc_now
    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.app.conversations import handle_conversation_search
    from server.knowledge.store import conversations as repo

    org = unique_org("unsave-peer")
    _recording(monkeypatch, False)
    await _save(org, namespace, content=content)
    await repo.append(
        repo.MessageInsert(
            org=org,
            namespace=namespace,
            agent="recorded-peer",
            user_id="peer-user",
            thread_id="c-1",
            msg_id="m-1",
            role="user",
            content=content,
            run_id="peer-run",
            ts=naive_utc_now(),
        )
    )
    if legacy_excerpt:
        await ch.execute(
            "ALTER TABLE loom.conversation_embeddings UPDATE content_excerpt = {excerpt:String} "
            "WHERE org = {org:String} AND namespace = {ns:String} SETTINGS mutations_sync = 2",
            params={"org": org, "ns": namespace, "excerpt": content[:240]},
        )
    before = await ch.query_one(
        "SELECT ts FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
    )
    assert before is not None

    if interrupted:
        real_unsave = repo.unsave_message

        async def fail_transcript(table: str, *args: Any, **kwargs: Any) -> None:
            if table == "loom.conversations":
                raise ConnectionError("transcript delete interrupted")
            await real_unsave(table, *args, **kwargs)

        monkeypatch.setattr(repo, "unsave_message", fail_transcript)
        with pytest.raises(ConnectionError, match="transcript delete interrupted"):
            await _unsave(org, namespace)
        assert await repo.saved_message_owner(org, namespace, "itest-client", "c-1", "m-1") == "u-1"
        handed_off = await ch.query_one(
            "SELECT agent FROM loom.conversation_embeddings FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL",
            params={"org": org, "ns": namespace},
        )
        assert handed_off == {"agent": "recorded-peer"}
        monkeypatch.setattr(repo, "unsave_message", real_unsave)

    assert (await _unsave(org, namespace)).removed
    assert await _saved_ids(org, namespace) == []
    result = await handle_conversation_search(
        ConversationSearchInput(
            org=org,
            namespace=namespace,
            agent="recorded-peer",
            user_id="peer-user",
            role="user",
            query=content,
            namespace_scope="this",
        )
    )
    assert [hit.msg_id for hit in result.hits] == ["m-1"]
    rows = await ch.query_all(
        "SELECT agent, user_id, role, run_id, ts, is_saved, saved_at "
        "FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL",
        params={"org": org, "ns": namespace},
    )
    assert rows == [
        {
            "agent": "recorded-peer",
            "user_id": "peer-user",
            "role": "user",
            "run_id": "peer-run",
            "ts": before["ts"],
            "is_saved": False,
            "saved_at": None,
        }
    ]
