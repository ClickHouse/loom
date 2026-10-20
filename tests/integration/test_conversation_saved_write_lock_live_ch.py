"""Saved-state commits share a lease with compaction's embedding retirement."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"), reason="LOOM_CH_HOST is not set"
)


@pytest.mark.asyncio
async def test_a_save_waits_for_compaction_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.llm.client
    from server.core.ch import get_client
    from server.core.datetimes import naive_utc_now
    from server.knowledge.api.models_conversations import (
        ConversationCompactInput,
        ConversationSaveMessageInput,
    )
    from server.knowledge.app import conversations as app
    from server.knowledge.store import conversations as repo
    from server.llm.client import LLMResult
    from tests.integration._resources import unique_org

    ch = get_client()
    await ch.connect()
    org = unique_org("saved-lock")
    namespace, thread_id, agent = "n", "t", "a"
    now = naive_utc_now()
    for i in range(8):
        await repo.append(
            repo.MessageInsert(
                org=org,
                namespace=namespace,
                agent=agent,
                thread_id=thread_id,
                msg_id=f"m{i}",
                role="assistant",
                content=f"answer {i}",
                user_id="u",
                ts=now - timedelta(seconds=8 - i),
            )
        )

    class LLM:
        provider = model = "fake"

        async def complete(self, **kw: Any) -> LLMResult:
            return LLMResult(
                text="The answers were discussed.",
                tokens_in=1,
                tokens_out=1,
                provider="fake",
                model="fake",
            )

    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: LLM())
    retirement_started, retire = asyncio.Event(), asyncio.Event()
    save_attempted, save_entered = asyncio.Event(), asyncio.Event()
    real_retire, real_lock = repo.tombstone_embeddings, repo.saved_message_write_lock

    async def paused_retirement(*args: Any, **kwargs: Any) -> None:
        retirement_started.set()
        await retire.wait()
        await real_retire(*args, **kwargs)

    @asynccontextmanager
    async def observed_lock(*args: Any):
        saving = asyncio.current_task().get_name() == "save-during-retirement"
        if saving:
            save_attempted.set()
        async with real_lock(*args):
            if saving:
                save_entered.set()
            yield

    monkeypatch.setattr(repo, "tombstone_embeddings", paused_retirement)
    monkeypatch.setattr(repo, "saved_message_write_lock", observed_lock)
    compact = asyncio.create_task(
        app.handle_conversation_compact(
            ConversationCompactInput(
                org=org,
                namespace=namespace,
                agent=agent,
                thread_id=thread_id,
                keep_recent=2,
                min_to_compact=2,
            )
        )
    )
    save = None
    try:
        await asyncio.wait_for(retirement_started.wait(), 10)
        save = asyncio.create_task(
            app.handle_conversation_save_message(
                ConversationSaveMessageInput(
                    org=org,
                    namespace=namespace,
                    agent=agent,
                    conversation_id=thread_id,
                    message_id="m1",
                    role="assistant",
                    content="answer 1",
                    user_id="u",
                )
            ),
            name="save-during-retirement",
        )
        await asyncio.wait_for(save_attempted.wait(), 10)
        assert not save_entered.is_set()
        retire.set()
        result, saved = await asyncio.wait_for(asyncio.gather(compact, save), 10)
        assert result.compacted
        assert not saved.indexed and saved.reason == "deleted"
        row = await ch.query_one(
            "SELECT is_saved, tombstoned_at FROM loom.conversations "
            "WHERE org = {org:String} AND namespace = {ns:String} AND msg_id = 'm1'",
            params={"org": org, "ns": namespace},
        )
        assert row and not row["is_saved"] and row["tombstoned_at"] is not None
    finally:
        retire.set()
        for task in (compact, save):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (compact, save) if task), return_exceptions=True)
        await ch.disconnect()
