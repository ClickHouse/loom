"""OpenAI text-embedding-3-* embedder. Requires OPENAI_API_KEY."""

from __future__ import annotations

from typing import Any, ClassVar

from server.clients._http import get_model_client as _http_client
from server.clients._http_errors import raise_for_status_with_detail as _raise_for_status
from server.core.provider_keys import provider_key
from server.embedding._cache import cached_embed, last_tier
from server.embedding._provider import (
    EmbedderInputTooLarge,
    MalformedEmbedding,
    check_size,
    count_tokens,
    filled,
    retrying,
    split_cached,
    store,
    vector,
)
from server.llm import customer, gateway
from server.observability.genai import embedding_span
from server.observability.provider_health import recording

__all__ = ["EmbedderInputTooLarge", "OpenAIEmbedder"]

# OpenAI's documented input cap for text-embedding-3-*. Tokens, not chars.
_MAX_INPUT_TOKENS = 8191
_OPENAI_BATCH_LIMIT = 100
_HEALTH = "openai:embeddings"


def _report_billed(model: str, data: dict[str, Any]) -> int:
    """The response's billed token count, reported to the active op's cost."""
    try:
        from server.observability.cost_accumulator import report_embed

        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        if tokens > 0:
            report_embed(model, tokens)
        return tokens
    except Exception:  # noqa: BLE001 — cost reporting must not fail the embed
        return 0


class OpenAIEmbedder:
    # The most tokens one input may hold; a longer one is refused before the call.
    max_input_tokens: ClassVar[int] = _MAX_INPUT_TOKENS
    DEFAULT_DIMS: ClassVar[dict[str, int]] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
    }

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        dim: int | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.dim = dim or self.DEFAULT_DIMS.get(model, 1536)
        self._api_key = provider_key("openai") if api_key is None else api_key
        if not self._api_key and not gateway.keyless():
            raise RuntimeError(
                "OPENAI_API_KEY not set; OpenAIEmbedder requires a key. "
                "Use LOOM_EMBEDDING_MODEL=fake-ngram-3072 for local dev."
            )
        # The billed count of the last single-text provider call. The span must
        # carry the provider's `usage` number, not a local estimate, or it and
        # `agent_events.upstream_cost_usd` disagree. Safe on a shared instance:
        # nothing awaits between the fetch setting it and `embed` reading it.
        self._last_billed_tokens = 0

    def _count_tokens(self, text: str) -> int:
        return count_tokens(text)

    def _pinned_width(self) -> int | None:
        """The width the request asks for; ada-002 takes no `dimensions`."""
        return self.dim if self.dim and self.model_name != "text-embedding-ada-002" else None

    def _payload(self, text: str | list[str]) -> dict[str, object]:
        payload: dict[str, object] = {"model": self.model_name, "input": text}
        width = self._pinned_width()
        if width:
            payload["dimensions"] = width
        return payload

    async def _post(self, payload: dict[str, object], who: customer.Customer) -> dict[str, Any]:
        from server.settings import settings

        url = f"{settings.openai_base_url.rstrip('/')}/embeddings"
        bearer = await gateway.bearer(self._api_key, url)
        resp = await _http_client().post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {bearer}",
                **customer.headers(who),
            },
        )
        _raise_for_status(resp, _HEALTH)
        data: dict[str, Any] = resp.json()
        return data

    async def _embed_one_raw(self, text: str) -> list[float]:
        # Outside `recording`: an oversized text, or a call naming no customer,
        # is the caller's to fix, not a provider failure for /v1/health to report.
        who = await customer.require()
        check_size(text, _MAX_INPUT_TOKENS, "OpenAI text-embedding-3")
        with recording(_HEALTH):
            return await self._embed_one_raw_inner(text, who)

    async def _embed_one_raw_inner(self, text: str, who: customer.Customer) -> list[float]:
        async for attempt in retrying():
            with attempt:
                data = await self._post(self._payload(text), who)
                self._last_billed_tokens = _report_billed(self.model_name, data)
                return vector(data["data"][0]["embedding"], self._pinned_width())
        raise RuntimeError("embedding retry loop exited without result")

    async def embed(self, text: str) -> list[float]:
        with embedding_span(provider="openai", model=self.model_name) as _emb:
            self._last_billed_tokens = 0
            vec, was_hit = await cached_embed(
                self.model_name, text, self._embed_one_raw, dim=self.dim
            )
            _emb.set_tier(last_tier())
            # The estimate only prices the cache saving; nothing was billed.
            _emb.record_usage(
                billed_tokens=0 if was_hit else self._last_billed_tokens,
                cached_tokens=self._count_tokens(text) if was_hit else 0,
                cache_hits=1 if was_hit else 0,
                cache_misses=0 if was_hit else 1,
                called_provider=not was_hit,
            )
            return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One span for the whole batch: a span per input would bury the trace
        under siblings that share one HTTP round trip."""
        with embedding_span(provider="openai", model=self.model_name) as _emb:
            results, billed, hits = await self._embed_many_raw(texts)
            _emb.record_usage(
                billed_tokens=billed,
                cached_tokens=sum(self._count_tokens(t) for t in hits),
                cache_hits=len(hits),
                cache_misses=len(texts) - len(hits),
                batch_size=len(texts),
                called_provider=len(hits) < len(texts),
            )
            return results

    async def _embed_many_raw(self, texts: list[str]) -> tuple[list[list[float]], int, list[str]]:
        """`(vectors in input order, billed_tokens, cache_hit_texts)`; misses go
        to the API `_OPENAI_BATCH_LIMIT` at a time."""
        who = await customer.require()  # before the cache: a hit must not hide a missed path
        results, misses, hits = split_cached(self.model_name, texts)
        billed = 0
        for start in range(0, len(misses), _OPENAI_BATCH_LIMIT):
            batch = misses[start : start + _OPENAI_BATCH_LIMIT]
            # Reject the whole batch before any request, so one oversized
            # input cannot leave partial embeddings behind a 400.
            for _, text in batch:
                check_size(text, _MAX_INPUT_TOKENS, "OpenAI text-embedding-3")
            with recording(_HEALTH):
                async for attempt in retrying():
                    with attempt:
                        data = await self._post(self._payload([t for _, t in batch]), who)
                billed += _report_billed(self.model_name, data)
                self._store_batch(results, batch, list(data["data"]))
        return filled(results), billed, hits

    def _store_batch(
        self,
        results: list[list[float] | None],
        batch: list[tuple[int, str]],
        entries: list[dict[str, Any]],
    ) -> None:
        if len(entries) != len(batch):
            raise MalformedEmbedding(
                f"embedding batch size mismatch: sent {len(batch)}, got {len(entries)}"
            )
        width = self._pinned_width()
        # Map by the documented `index`, not position: memory.set batches
        # [value, memory_key], and a reordered response would silently put
        # the key's vector in the value column.
        for pos, entry in enumerate(entries):
            try:
                slot = int(entry.get("index", pos))
            except (TypeError, ValueError):
                slot = pos
            if not 0 <= slot < len(batch):
                slot = pos
            store(self.model_name, results, *batch[slot], vector(entry["embedding"], width))
