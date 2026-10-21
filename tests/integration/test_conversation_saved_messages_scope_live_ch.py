"""Saved-message search under a token bounded to one user, against live
ClickHouse: the scope's predicate rides the statement, so a body naming
another user finds nothing of theirs.

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

_CONTENT = "the quarterly forecast assumes a flat churn rate"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def namespace() -> str:
    return f"itest-saved-scope-{uuid.uuid4().hex[:12]}"


async def _save(namespace: str, user_id: str, message_id: str) -> None:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_save_message

    out = await handle_conversation_save_message(
        ConversationSaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id=f"c-{user_id}",
            message_id=message_id,
            role="assistant",
            content=_CONTENT,
            user_id=user_id,
        )
    )
    assert out.indexed is True


async def _search(namespace: str, body_user: str) -> list[str]:
    from server.knowledge.api.models_conversations import ConversationSearchSavedMessagesInput
    from server.knowledge.app.conversations import handle_conversation_search_saved_messages

    out = await handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            user_id=body_user,
            query=_CONTENT,
        )
    )
    return [h.message_id for h in out.hits]


@pytest.mark.asyncio
async def test_a_user_bounded_scope_never_reaches_another_users_saved_message(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound

    await _save(namespace, "u-1", "m-u1")
    await _save(namespace, "u-2", "m-u2")

    u2 = ReadFilters(scope="own_user", user_id="u-2", session_id=None)
    with read_filters_bound(None, u2):
        assert await _search(namespace, "u-2") == ["m-u2"]
        # The transport refuses this body for a pinned token; were it to reach
        # the handler, the scope predicate still keeps u-1's row out.
        assert await _search(namespace, "u-1") == []


@pytest.mark.asyncio
async def test_the_substring_fallback_carries_the_same_scope(
    ch: Any, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound
    from server.knowledge.app import conversations

    await _save(namespace, "u-1", "m-u1")
    await _save(namespace, "u-2", "m-u2")

    class _DownEmbedder:
        model_name = "down"
        dim = 0

        async def embed(self, _text: str) -> list[float]:
            raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(conversations, "get_embedder", lambda: _DownEmbedder())
    u2 = ReadFilters(scope="own_user", user_id="u-2", session_id=None)
    with read_filters_bound(None, u2):
        assert await _search(namespace, "u-2") == ["m-u2"]
        assert await _search(namespace, "u-1") == []
