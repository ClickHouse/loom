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
