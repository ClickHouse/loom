"""Hybrid saved-message search against live ClickHouse: the BM25 leg reads a
saved message's whole text, so an exact term deep in a long answer ranks, and
it stays inside the user's own saved rows.

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

# Longer than the 2,048 characters the vector leg embeds; the ticket id sits
# past that cut.
_LONG = ("The rollout plan covers staging, canary and the full fleet. " * 60) + (
    "The incident ticket for the failed canary is INC-48213 on table events_raw."
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
    return f"itest-saved-hybrid-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _unrecorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.store import conversations as conversation_repo

    async def off(*_a: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", off)


async def _save(namespace: str, message_id: str, content: str, user_id: str = "u-1") -> None:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_save_message

    out = await handle_conversation_save_message(
        ConversationSaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-1",
            message_id=message_id,
            role="assistant",
            content=content,
            user_id=user_id,
        )
    )
    assert out.indexed is True


async def _search(namespace: str, query: str, user_id: str = "u-1", **kw: Any) -> list[Any]:
    from server.knowledge.api.models_conversations import ConversationSearchSavedMessagesInput
    from server.knowledge.app.conversations import handle_conversation_search_saved_messages

    out = await handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            user_id=user_id,
            query=query,
            **kw,
        )
    )
    return out.hits


@pytest.mark.asyncio
async def test_an_identifier_past_the_embedded_prefix_ranks_first(namespace: str) -> None:
    assert len(_LONG) > 2048
    await _save(namespace, "m-long", _LONG)
    await _save(namespace, "m-short", "Staging deploys run every afternoon after the canary.")

    for mode in ("hybrid", "lexical"):
        hits = await _search(namespace, "INC-48213", search_mode=mode)
        assert hits and hits[0].message_id == "m-long", mode
        assert "INC-48213" in hits[0].content_excerpt, mode
    # Both messages were saved as assistant turns.
    assert await _search(namespace, "INC-48213", search_mode="lexical", role="user") == []
    assert await _search(namespace, "INC-48213", search_mode="lexical", role="assistant")


@pytest.mark.asyncio
async def test_the_lexical_leg_keeps_to_the_users_saved_rows(namespace: str) -> None:
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound
    from server.knowledge.api.models_conversations import ConversationUnsaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_unsave_message

    await _save(namespace, "m-1", _LONG, user_id="u-1")
    await _save(namespace, "m-2", _LONG, user_id="u-2")
    await _save(namespace, "m-3", _LONG, user_id="u-2")
    await handle_conversation_unsave_message(
        ConversationUnsaveMessageInput(
            org="itest-org",
            namespace=namespace,
            agent="itest-client",
            conversation_id="c-1",
            message_id="m-3",
            user_id="u-2",
        )
    )

    hits = await _search(namespace, "INC-48213", user_id="u-2", search_mode="lexical")
    assert [h.message_id for h in hits] == ["m-2"]
    with read_filters_bound(None, ReadFilters(scope="own_user", user_id="u-2", session_id=None)):
        assert await _search(namespace, "INC-48213", user_id="u-1", search_mode="lexical") == []
