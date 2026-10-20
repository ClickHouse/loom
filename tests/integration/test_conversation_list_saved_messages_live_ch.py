"""conversation.list_saved_messages against live ClickHouse: one user's saved
messages, newest first, paged by an opaque cursor, without other users' or
withdrawn ones.

Skips cleanly when LOOM_CH_HOST is unset. Each test writes under its own
namespace.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)


@pytest.fixture(autouse=True)
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def namespace() -> str:
    return f"itest-list-saved-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _unrecorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.store import conversations as conversation_repo

    async def off(*_a: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", off)


async def _save(namespace: str, user_id: str, conversation_id: str, message_id: str) -> None:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_save_message

    out = await handle_conversation_save_message(
        ConversationSaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id=conversation_id,
            message_id=message_id,
            role="assistant",
            content=f"saved answer {message_id}",
            user_id=user_id,
        )
    )
    assert out.indexed is True
    # Distinct saved_at keys, so the newest-first order is deterministic.
    await asyncio.sleep(0.01)


async def _list(namespace: str, user_id: str, *, org: str = "itest-org", **kw: Any) -> Any:
    from server.knowledge.api.models_conversations import ConversationListSavedMessagesInput
    from server.knowledge.app.conversations import handle_conversation_list_saved_messages

    return await handle_conversation_list_saved_messages(
        ConversationListSavedMessagesInput(
            org=org, namespace=namespace, agent="itest-client", user_id=user_id, **kw
        )
    )


@pytest.mark.asyncio
async def test_pages_newest_first_and_stops_at_the_last_page(namespace: str) -> None:
    for mid in ("m-1", "m-2", "m-3"):
        await _save(namespace, "u-1", "c-1", mid)
    await _save(namespace, "u-2", "c-9", "m-other")

    first = await _list(namespace, "u-1", limit=2)
    assert [r.message_id for r in first.rows] == ["m-3", "m-2"]
    assert first.next_cursor

    second = await _list(namespace, "u-1", limit=2, cursor=first.next_cursor)
    assert [r.message_id for r in second.rows] == ["m-1"]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_filters_by_conversation_and_drops_withdrawn_messages(namespace: str) -> None:
    from server.knowledge.api.models_conversations import ConversationUnsaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_unsave_message

    await _save(namespace, "u-1", "c-1", "m-1")
    await _save(namespace, "u-1", "c-2", "m-2")
    await _save(namespace, "u-1", "c-2", "m-3")
    await handle_conversation_unsave_message(
        ConversationUnsaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-2",
            message_id="m-3",
            user_id="u-1",
        )
    )

    out = await _list(namespace, "u-1", conversation_id="c-2")
    assert [(r.conversation_id, r.message_id) for r in out.rows] == [("c-2", "m-2")]


@pytest.mark.asyncio
async def test_a_user_bounded_scope_lists_nothing_of_another_user(namespace: str) -> None:
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound

    await _save(namespace, "u-1", "c-1", "m-1")
    with read_filters_bound(None, ReadFilters(scope="own_user", user_id="u-2", session_id=None)):
        assert (await _list(namespace, "u-1")).rows == []


@pytest.mark.asyncio
async def test_pages_by_save_time_independently_of_transcript_time(ch: Any, namespace: str) -> None:
    org = unique_org("saved-order")
    message_time = datetime(2026, 1, 1)
    first_save = message_time + timedelta(days=2)
    last_save = first_save + timedelta(hours=1)
    rows = [
        ("c-1", "newer-message", message_time + timedelta(days=1), first_save),
        ("c-1", "older-message", message_time, last_save),
        ("c-2", "shared-id", message_time, last_save),
        ("c-3", "shared-id", message_time, last_save),
        ("c-4", "withdrawn", message_time + timedelta(days=4), None),
    ]
    await ch.insert(
        "loom.conversations",
        [
            {
                "org": org,
                "namespace": namespace,
                "agent": "itest-client",
                "user_id": "u-1",
                "thread_id": tid,
                "msg_id": mid,
                "role": "assistant",
                "content": mid,
                "is_saved": True,
                "ts": written,
                "saved_at": saved,
            }
            for tid, mid, written, saved in rows
        ],
    )

    first = await _list(namespace, "u-1", org=org, limit=1)
    assert [(r.conversation_id, r.message_id, r.ts) for r in first.rows] == [
        ("c-3", "shared-id", last_save)
    ]
    assert first.next_cursor
    second = await _list(namespace, "u-1", org=org, limit=1, cursor=first.next_cursor)
    assert [(r.conversation_id, r.message_id, r.ts) for r in second.rows] == [
        ("c-2", "shared-id", last_save)
    ]
    assert second.next_cursor
    last = await _list(namespace, "u-1", org=org, limit=2, cursor=second.next_cursor)
    assert [(r.conversation_id, r.message_id, r.ts) for r in last.rows] == [
        ("c-1", "older-message", last_save),
        ("c-1", "newer-message", first_save),
    ]
    assert last.next_cursor is None
