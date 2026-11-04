"""Google Gemini embedder (gemini-embedding-001). Requires GEMINI_API_KEY or
GOOGLE_API_KEY.

Generative Language API: `:embedContent` for one text, `:batchEmbedContents`
for a batch. The key rides the `x-goog-api-key` header, never the URL, so it
stays out of request logs. Responses carry no usage block: billed tokens are
always 0, and the token count is a cl100k estimate used only for the size
guard and the cache-savings figure.
"""

from __future__ import annotations

from typing import Any, ClassVar

from server.clients._http import get_model_client as _http_client
from server.clients._http_errors import raise_for_status_with_detail as _raise_for_status
from server.core.provider_keys import provider_key
from server.embedding._cache import cached_embed, last_tier
from server.embedding._provider import (
    MalformedEmbedding,
    check_size,
    count_tokens,
    filled,
    retrying,
    split_cached,
    store,
    vector,
)
from server.llm import customer
from server.observability.genai import embedding_span
from server.observability.provider_health import recording

# gemini-embedding-001's documented input cap. Tokens, not chars.
_MAX_INPUT_TOKENS = 2048
_GEMINI_BATCH_LIMIT = 100
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_HEALTH = "gemini:embeddings"


class GeminiEmbedder:
    # The most tokens one input may hold; a longer one is refused before the call.
    max_input_tokens: ClassVar[int] = _MAX_INPUT_TOKENS
    DEFAULT_DIMS: ClassVar[dict[str, int]] = {
        "gemini-embedding-001": 3072,
    }

    def __init__(
        self,
        *,
        model: str = "gemini-embedding-001",
        dim: int | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.dim = dim or self.DEFAULT_DIMS.get(model, 3072)
        key = provider_key("gemini") if api_key is None else api_key
        if not key:
            raise RuntimeError(
                "neither GEMINI_API_KEY nor GOOGLE_API_KEY is set; "
                "GeminiEmbedder requires a key. Use "
                "LOOM_EMBEDDING_MODEL=fake-ngram-3072 for local dev."
            )
        self._api_key: str = key
        self._endpoint = f"{_API_ROOT}/{model}:embedContent"
        self._batch_endpoint = f"{_API_ROOT}/{model}:batchEmbedContents"

    def _count_tokens(self, text: str) -> int:
        return count_tokens(text)

    def _content_request(self, text: str) -> dict[str, object]:
        # The batch API needs the qualified `models/<name>` in every request;
        # the single API accepts it.
        return {
            "model": f"models/{self.model_name}",
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": self.dim,
        }

    async def _post(
        self, url: str, payload: dict[str, object], who: customer.Customer
    ) -> dict[str, Any]:
        resp = await _http_client().post(
            url,
            json=payload,
            headers={"x-goog-api-key": self._api_key, **customer.headers(who)},
        )
        _raise_for_status(resp, _HEALTH)
        data: dict[str, Any] = resp.json()
        return data

    async def _embed_one_raw(self, text: str) -> list[float]:
        # Outside `recording`: an oversized text, or a call naming no customer,
        # is the caller's to fix, not a provider failure for /v1/health to report.
        who = await customer.require()
        check_size(text, _MAX_INPUT_TOKENS, "gemini-embedding-001")
        with recording(_HEALTH):
            return await self._embed_one_raw_inner(text, who)

    async def _embed_one_raw_inner(self, text: str, who: customer.Customer) -> list[float]:
        async for attempt in retrying():
            with attempt:
                data = await self._post(self._endpoint, self._content_request(text), who)
                return vector(data["embedding"]["values"], self.dim)
        raise RuntimeError("embedding retry loop exited without result")

    async def embed(self, text: str) -> list[float]:
        with embedding_span(provider="gemini", model=self.model_name) as _emb:
            vec, was_hit = await cached_embed(
                self.model_name, text, self._embed_one_raw, dim=self.dim
            )
            _emb.set_tier(last_tier())
            _emb.record_usage(
                billed_tokens=0,
                cached_tokens=self._count_tokens(text) if was_hit else 0,
                cache_hits=1 if was_hit else 0,
                cache_misses=0 if was_hit else 1,
                called_provider=not was_hit,
            )
            return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One span for the whole batch: a span per input would bury the trace
        under siblings that share one HTTP round trip."""
        with embedding_span(provider="gemini", model=self.model_name) as _emb:
            results, hits = await self._embed_many_raw(texts)
            _emb.record_usage(
                billed_tokens=0,
                cached_tokens=sum(self._count_tokens(t) for t in hits),
                cache_hits=len(hits),
                cache_misses=len(texts) - len(hits),
                batch_size=len(texts),
                called_provider=len(hits) < len(texts),
            )
            return results

    async def _embed_many_raw(self, texts: list[str]) -> tuple[list[list[float]], list[str]]:
        """`(vectors in input order, cache_hit_texts)`; misses go to the API
        `_GEMINI_BATCH_LIMIT` at a time."""
        who = await customer.require()  # before the cache: a hit must not hide a missed path
        results, misses, hits = split_cached(self.model_name, texts)
        for start in range(0, len(misses), _GEMINI_BATCH_LIMIT):
            batch = misses[start : start + _GEMINI_BATCH_LIMIT]
            # Reject the whole batch before any request, so one oversized
            # input cannot leave partial embeddings behind a 400.
            for _, text in batch:
                check_size(text, _MAX_INPUT_TOKENS, "gemini-embedding-001")
            payload: dict[str, object] = {"requests": [self._content_request(t) for _, t in batch]}
            with recording(_HEALTH):
                async for attempt in retrying():
                    with attempt:
                        data = await self._post(self._batch_endpoint, payload, who)
                # batchEmbedContents answers in request order with no per-entry
                # index, so map by position; a short answer is a hard failure,
                # never a silent misalignment.
                entries = list(data["embeddings"])
                if len(entries) != len(batch):
                    raise MalformedEmbedding(
                        f"embedding batch size mismatch: sent {len(batch)}, got {len(entries)}"
                    )
                for (i, text), entry in zip(batch, entries, strict=True):
                    store(self.model_name, results, i, text, vector(entry["values"], self.dim))
        return filled(results), hits
