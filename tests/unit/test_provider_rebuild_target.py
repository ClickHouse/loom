"""Explicit rebuild clients bypass serving caches and await all raw requests."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from server.insights.app import provider_rebuild_target as target


@pytest.mark.asyncio
async def test_raw_target_preserves_input_order_and_never_uses_cached_embed(monkeypatch):
    raw = AsyncMock(side_effect=lambda text: [float(len(text)), 1.0])
    client = Mock(_embed_one_raw=raw)
    factory = Mock(return_value=client)
    monkeypatch.setattr(target, "OpenAIEmbedder", factory)
    monkeypatch.setattr(target, "environment_provider_key", lambda _: "environment-key")
    instance = target.rebuild_target(("openai", "model", 2, "", False), None)
    assert await instance.embed_many(["a", "bb"]) == [[1.0, 1.0], [2.0, 1.0]]
    factory.assert_called_once_with(model="model", dim=2, api_key="environment-key")
    client.embed.assert_not_called()
    client.embed_many.assert_not_called()


@pytest.mark.asyncio
async def test_failed_raw_batch_cancels_and_awaits_siblings():
    finished = asyncio.Event()

    async def fetch(text):
        if text == "bad":
            await asyncio.sleep(0)
            raise ValueError("provider failed")
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    instance = target.RebuildEmbedder("model", 2, fetch)
    with pytest.raises(ExceptionGroup):
        await instance.embed_many(["good", "bad"])
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["deferred", "identity", "switch"])
async def test_a_raw_identity_hold_drains_its_siblings_and_keeps_its_type(kind):
    from server.core.errors import ModelCallDeferred
    from server.llm.customer import SwitchUnread
    from server.llm.identity import IdentityUnread

    error = {
        "deferred": ModelCallDeferred("wait", hold_s=600, retry_after_s=600),
        "identity": IdentityUnread("directory unavailable"),
        "switch": SwitchUnread("switch unavailable"),
    }[kind]
    started, finished = asyncio.Event(), asyncio.Event()

    async def fetch(text):
        if text == "held":
            await started.wait()
            raise error
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    with pytest.raises(type(error)) as caught:
        await target.RebuildEmbedder("model", 2, fetch).embed_many(["sibling", "held"])
    assert caught.value is error
    assert finished.is_set()


@pytest.mark.asyncio
async def test_a_mixed_raw_failure_keeps_the_provider_error():
    from server.core.errors import ModelCallDeferred

    ready = asyncio.Event()
    started = 0

    async def fetch(text):
        nonlocal started
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()
        if text == "held":
            raise ModelCallDeferred("wait", hold_s=600, retry_after_s=600)
        raise ValueError("provider failed")

    with pytest.raises(ExceptionGroup) as caught:
        await target.RebuildEmbedder("model", 2, fetch).embed_many(["held", "bad"])
    assert caught.value.subgroup(ValueError) is not None


@pytest.mark.asyncio
async def test_cancelling_a_raw_batch_finishes_its_requests():
    started, finished = asyncio.Event(), asyncio.Event()

    async def fetch(text):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    batch = asyncio.create_task(target.RebuildEmbedder("model", 2, fetch).embed_many(["a"]))
    await started.wait()
    batch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("unread", [False, True])
async def test_nested_raw_holds_keep_the_longest_wait_or_the_unread_switch(unread):
    from server.core.errors import ModelCallDeferred
    from server.llm.customer import SwitchUnread

    short = ModelCallDeferred("wait", hold_s=5, retry_after_s=5)
    long = ModelCallDeferred("wait", hold_s=600, retry_after_s=600)
    switch = SwitchUnread("switch unavailable")
    causes = [long, switch] if unread else [long]

    async def fetch(text):
        raise ExceptionGroup("raw holds", [short, ExceptionGroup("nested", causes)])

    wanted = switch if unread else long
    with pytest.raises(type(wanted)) as caught:
        await target.RebuildEmbedder("model", 2, fetch).embed_many(["held"])
    assert caught.value is wanted
