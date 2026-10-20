"""conversation.saved_status against live ClickHouse: one call names which of
a conversation's messages a user saved, without other users' saves, withdrawn
ones, or the same message id in another conversation.

Skips cleanly when LOOM_CH_HOST is unset. Each test writes under its own
namespace.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

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
    return f"itest-saved-status-{uuid.uuid4().hex[:12]}"


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
            role="user",
            content=f"saved {message_id}",
            user_id=user_id,
        )
    )
    assert out.indexed is True


async def _status(namespace: str, user_id: str, conversation_id: str, ids: list[str]) -> list[str]:
    from server.knowledge.api.models_conversations import ConversationSavedStatusInput
    from server.knowledge.app.conversations import handle_conversation_saved_status

    out = await handle_conversation_saved_status(
        ConversationSavedStatusInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            user_id=user_id,
            conversation_id=conversation_id,
            message_ids=ids,
        )
    )
    return out.saved


@pytest.mark.asyncio
async def test_names_only_this_users_saves_in_this_conversation(namespace: str) -> None:
    from server.knowledge.api.models_conversations import ConversationUnsaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_unsave_message

    await _save(namespace, "u-1", "c-1", "m-1")
    await _save(namespace, "u-1", "c-1", "m-3")
    await _save(namespace, "u-1", "c-1", "m-4")
    await _save(namespace, "u-2", "c-1", "m-2")
    await _save(namespace, "u-1", "c-other", "m-5")
    await handle_conversation_unsave_message(
        ConversationUnsaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-1",
            message_id="m-4",
            user_id="u-1",
        )
    )

    asked = ["m-5", "m-4", "m-3", "m-2", "m-1", "m-missing"]
    assert await _status(namespace, "u-1", "c-1", asked) == ["m-3", "m-1"]


@pytest.mark.asyncio
async def test_a_user_bounded_scope_sees_no_other_users_saves(namespace: str) -> None:
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound

    await _save(namespace, "u-1", "c-1", "m-1")
    with read_filters_bound(None, ReadFilters(scope="own_user", user_id="u-2", session_id=None)):
        assert await _status(namespace, "u-1", "c-1", ["m-1"]) == []
