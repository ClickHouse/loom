"""Explicitly configured OpenAI-compatible embedding endpoints.

Compatibility means the embeddings wire format, not OpenAI pricing, token
limits, model names or support for the optional dimensions parameter.
"""

import hashlib
from urllib.parse import urlsplit, urlunsplit

from server.clients._http import get_client
from server.embedding._cache import cached_embed
from server.embedding._provider import MalformedEmbedding, retrying, vector


def endpoint_url(base_url: str) -> str:
    """Accept an operator-supplied API root without credentials in its URL.

    Private HTTP endpoints are intentional for self-hosted Docker services.
    Only deployment operators may set this value; it is not request input.
    Redirects are refused by the client so credentials stay at this endpoint.
    """
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Embedding API root must be HTTP(S), without credentials, query or fragment"
        )
    # Accessing port validates malformed / out-of-range ports before any request.
    _ = parsed.port
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/embeddings", "", "")
    )


class CompatibleEmbedder:
    """A strict embeddings-protocol adapter; returned vectors must match dim."""

    def __init__(
        self,
        *,
        model: str,
        dim: int,
        base_url: str,
        api_key: str,
        send_dimensions: bool = False,
    ) -> None:
        if not model.strip() or dim < 1:
            raise ValueError("An embedding model and positive dimensions are required")
        self.model_name = model
        self.dim = dim
        self._endpoint = endpoint_url(base_url)
        self._key = api_key
        self._send_dimensions = send_dimensions
        # A model name can exist at several endpoints with different weights.
        # Never reuse another endpoint's cached vectors or encode its API key.
        identity = f"{self._endpoint}\n{model}\n{dim}\n{send_dimensions}"
        self._cache_model = "compatible:" + hashlib.sha256(identity.encode()).hexdigest()

    async def _request(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.model_name,
            "input": texts,
            "encoding_format": "float",
        }
        if self._send_dimensions:
            payload["dimensions"] = self.dim
        from server.llm import gateway

        gateway.refuse_provider_api(self._endpoint)
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        async for attempt in retrying():
            with attempt:
                response = await get_client().post(
                    self._endpoint, json=payload, headers=headers, follow_redirects=False
                )
                response.raise_for_status()
                data = response.json()
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list) or len(entries) != len(texts):
            raise MalformedEmbedding("embedding response count does not match input count")
        results: dict[int, list[float]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise MalformedEmbedding("invalid embedding response entry")
            slot = entry.get("index")
            if type(slot) is not int or not 0 <= slot < len(texts) or slot in results:
                raise MalformedEmbedding("embedding response indices must be unique and in range")
            values = entry.get("embedding")
            if not isinstance(values, list):
                raise MalformedEmbedding("embedding response must contain float arrays")
            result = vector(values, self.dim)
            if not any(result):
                raise MalformedEmbedding("embedding response contains a zero vector")
            results[slot] = result
        return [results[index] for index in range(len(texts))]

    async def _embed_one_raw(self, text: str) -> list[float]:
        """Uncached credential probe and single-input embedding."""
        return (await self._request([text]))[0]

    async def embed(self, text: str) -> list[float]:
        result, _ = await cached_embed(self._cache_model, text, self._embed_one_raw, dim=self.dim)
        return result

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        # Conservative batch bound. Input limits remain the provider's contract;
        # do not impose an OpenAI tokenizer on unrelated embedding models.
        for offset in range(0, len(texts), 32):
            results.extend(await self._request(texts[offset : offset + 32]))
        return results
