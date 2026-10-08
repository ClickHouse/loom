"""The LLM client an extraction's calls share: the deployment's extractor,
wrapped in the exact-prompt response cache when it is
on for the request, and the usage accounting across retries.
"""

from __future__ import annotations

from typing import Any

from server.llm.client import LLMResult, get_llm_client


def extractor_llm_cache_on(override: bool | None = None) -> bool:
    """Whether the extractor's calls go through the exact-prompt
    response cache. A per-request pin wins; else the traffic
    class's policy when it names `llm_cache` (`bench` on by
    default); else off — production extraction is not cached."""
    if override is not None:
        return bool(override)
    try:
        from server.identity.domain.traffic_class_policy import llm_policy_for
        from server.observability.traffic_class import current_traffic_class

        class_default = llm_policy_for(current_traffic_class()).llm_cache
    except Exception:  # noqa: BLE001 — the policy never fails a write
        class_default = None
    return bool(class_default)


def _client_cache_counts(client: Any) -> dict[str, int]:
    """The wrapper's hit / miss counts over one extraction's calls; 0 / 0 for
    a bare provider client (the cache was off)."""
    return {
        "llm_cache_hits": int(getattr(client, "hits", 0) or 0),
        "llm_cache_misses": int(getattr(client, "misses", 0) or 0),
    }


def _extractor_client(*, org: str = "", namespace: str = "", llm_cache: bool | None = None) -> Any:
    """The client every call of one extraction shares: the deployment's
    extractor, wrapped in `CachedLLMClient` when the cache is on for this
    request. One wrapper per extraction so its counters are the call's own."""
    client = get_llm_client()
    if extractor_llm_cache_on(llm_cache):
        from server.llm.cache import CachedLLMClient

        return CachedLLMClient(client, org=org, namespace=namespace)
    return client


def _merge_llm_results(a: LLMResult, b: LLMResult, *, text: str | None = None) -> LLMResult:
    """Accumulate token spend across an extraction's calls — every token count the
    provider reported; the text is `text` when given, else the latest call's."""
    return LLMResult(
        text=b.text if text is None else text,
        tokens_in=a.tokens_in + b.tokens_in,
        tokens_out=a.tokens_out + b.tokens_out,
        provider=b.provider,
        model=b.model,
        cached_input_tokens=a.cached_input_tokens + b.cached_input_tokens,
        cache_write_input_tokens=a.cache_write_input_tokens + b.cache_write_input_tokens,
        stop_reason=b.stop_reason,
    )
