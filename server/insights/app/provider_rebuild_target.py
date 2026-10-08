"""Explicit, uncached target clients. Never consult old serving-vector caches."""

import asyncio
from collections.abc import Awaitable, Callable

from server.core.provider_keys import environment_provider_key
from server.embedding.compatible import CompatibleEmbedder
from server.embedding.fake import FakeNGramEmbedder
from server.embedding.gemini import GeminiEmbedder
from server.embedding.local import LocalEmbedder
from server.embedding.openai import OpenAIEmbedder
from server.insights.api.models_provider_setup import ProviderSetupInput


class RebuildEmbedder:
    """Raw provider requests, at most eight concurrently, without cache writes."""

    def __init__(
        self, model: str, dim: int, fetch: Callable[[str], Awaitable[list[float]]]
    ) -> None:
        self.model_name, self.dim, self.fetch = model, dim, fetch

    async def embed(self, text: str) -> list[float]:
        return await self.fetch(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        semaphore = asyncio.Semaphore(8)

        async def one(text: str) -> list[float]:
            async with semaphore:
                return await self.fetch(text)

        # TaskGroup waits for sibling requests to finish cancellation before an
        # error escapes; no provider request may outlive the coordinator fence.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(text)) for text in texts]
        return [task.result() for task in tasks]


def rebuild_target(
    identity: tuple[str, str, int, str, bool], setup: ProviderSetupInput | None
) -> RebuildEmbedder:
    provider, model, dim, url, send_dimensions = identity
    key = (
        setup.keys[provider].get_secret_value()
        if setup is not None and provider in setup.keys
        else environment_provider_key(provider)
    )
    if provider == "openai":
        fetch = OpenAIEmbedder(model=model, dim=dim, api_key=key)._embed_one_raw
    elif provider == "gemini":
        fetch = GeminiEmbedder(model=model, dim=dim, api_key=key)._embed_one_raw
    elif provider == "compatible":
        fetch = CompatibleEmbedder(
            model=model, dim=dim, base_url=url, send_dimensions=send_dimensions, api_key=key
        )._embed_one_raw
    elif provider == "fake":
        fetch = FakeNGramEmbedder(dim=dim).embed
    elif provider == "local":
        fetch = LocalEmbedder(spec=model).embed
    else:
        raise ValueError("Unsupported rebuild provider")
    return RebuildEmbedder(model, dim, fetch)
