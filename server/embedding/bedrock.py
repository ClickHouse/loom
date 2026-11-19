"""Cohere Embed v4 over native Bedrock: query and document vectors stay distinct."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from server.clients import bedrock
from server.embedding._cache import cached_embed, last_tier
from server.embedding._provider import (
    MalformedEmbedding,
    check_size,
    count_tokens,
    filled,
    split_cached,
    store,
    vector,
)
from server.llm import customer
from server.observability.genai import embedding_span
from server.observability.provider_health import recording
from server.settings import settings

_BATCH_LIMIT = 96
_WIDTHS = frozenset({256, 512, 1024, 1536})


def _billed_tokens(model: str, response: dict[str, Any]) -> int | None:
    try:
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        units = (response.get("body", {}).get("meta") or {}).get("billed_units") or {}
        raw = headers.get("x-amzn-bedrock-input-token-count", units.get("input_tokens"))
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return None
        tokens = int(raw)
        if tokens < 0:
            return None
        if tokens:
            from server.observability.cost_accumulator import report_embed

            report_embed(model, tokens)
        return tokens
    except Exception:  # noqa: BLE001 — provider usage reporting must not fail an embedding
        return None


def _vectors(data: dict[str, Any], count: int, width: int) -> list[list[float]]:
    raw = data.get("embeddings")
    if isinstance(raw, dict):
        raw = raw.get("float")
    if not isinstance(raw, list) or len(raw) != count:
        raise MalformedEmbedding("Bedrock embedding count does not match input count")
    try:
        return [vector(values, width) for values in raw]
    except (TypeError, ValueError) as exc:
        raise MalformedEmbedding("Bedrock returned an invalid embedding vector") from exc


class BedrockEmbedder:
    max_input_tokens: ClassVar[int] = 128000

    def __init__(self, *, model: str, dim: int) -> None:
        if dim not in _WIDTHS:
            raise ValueError("Cohere Embed v4 dimensions must be 256, 512, 1024 or 1536")
        self.model_name, self.dim = model, dim
        self._model_id = bedrock.model_id(model)
        self._cache_model = f"bedrock:{self._model_id}:{dim}"

    async def _fetch(self, texts: list[str], purpose: str) -> tuple[list[list[float]], int | None]:
        who = await customer.require()
        for text in texts:
            check_size(text, self.max_input_tokens, self.model_name)
        payload = {
            "modelId": self._model_id,
            "accept": "application/json",
            "contentType": "application/json",
            "body": json.dumps(
                {
                    "texts": texts,
                    "input_type": purpose,
                    "embedding_types": ["float"],
                    "output_dimension": self.dim,
                    "truncate": "NONE",
                }
            ),
        }
        with recording("bedrock:embeddings"):
            data = await bedrock.call(
                "invoke_model",
                payload,
                who,
                request_timeout_s=settings.llm_request_timeout_s,
                attempts=settings.embedding_retry_attempts,
                max_wait_s=settings.embedding_retry_max_wait_s,
            )
            billed = _billed_tokens(self.model_name, data)
            return _vectors(data["body"], len(texts), self.dim), billed

    async def _embed_one_raw(self, text: str) -> list[float]:
        vectors, _ = await self._fetch([text], "search_query")
        return vectors[0]

    async def _embed_document_raw(self, text: str) -> list[float]:
        vectors, _ = await self._fetch([text], "search_document")
        return vectors[0]

    async def embed(self, text: str) -> list[float]:
        billed: int | None = None

        async def fetch(query: str) -> list[float]:
            nonlocal billed
            vectors, billed = await self._fetch([query], "search_query")
            return vectors[0]

        with embedding_span(provider="bedrock", model=self.model_name) as span:
            vec, hit = await cached_embed(self._cache_model, text, fetch, dim=self.dim)
            span.set_tier(last_tier())
            span.record_usage(
                billed_tokens=billed or 0,
                usage_known=billed is not None,
                cached_tokens=count_tokens(text) if hit else 0,
                cache_hits=int(hit),
                cache_misses=int(not hit),
                called_provider=not hit,
            )
            return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        await customer.require()
        for text in texts:
            check_size(text, self.max_input_tokens, self.model_name)
        with embedding_span(provider="bedrock", model=self.model_name) as span:
            results, misses, hits = split_cached(self._cache_model, texts)
            billed: int | None = 0
            for start in range(0, len(misses), _BATCH_LIMIT):
                batch = misses[start : start + _BATCH_LIMIT]
                vectors, tokens = await self._fetch([text for _, text in batch], "search_document")
                billed = billed + tokens if billed is not None and tokens is not None else None
                for (index, text), vec in zip(batch, vectors, strict=True):
                    store(self._cache_model, results, index, text, vec)
            span.record_usage(
                billed_tokens=billed or 0,
                usage_known=billed is not None,
                cached_tokens=sum(count_tokens(text) for text in hits),
                cache_hits=len(hits),
                cache_misses=len(misses),
                batch_size=len(texts),
                called_provider=bool(misses),
            )
            return filled(results)
