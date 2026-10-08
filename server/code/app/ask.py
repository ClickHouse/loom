"""code.ask: a synthesized answer over code.search's retrieval, memoized per question."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.api.models_search import CodeAskInput, CodeAskOutput
from server.code.app.common import (
    cache_outcome_label,
    index_as_of,
    new_trace_run_id,
    record_code_ask_cache,
    require_code_intel,
    resolve_code_identity,
    resolve_code_thresholds,
    schedule_code_trace,
)
from server.code.app.spans import set_code_span_attributes
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


def _ask_search_kwargs(
    input: CodeAskInput, abstain_threshold: float, noise_floor: float
) -> dict[str, Any]:
    """Retrieval settings code.ask grounds its synthesis on. The token
    budget is the caller's when given, else the deployment default — the
    judged bench sweeps it to place the tokens-vs-quality knee."""
    return {
        "top_k": 10,
        "token_budget": input.token_budget or settings.code_ask_token_budget,
        # The same rerank resolution as code.search ("" = the deployment default, "none" = off):
        # ask synthesizes FROM these excerpts.
        "rerank_endpoint": input.rerank,
        "abstain_threshold": abstain_threshold,
        "noise_floor": noise_floor,
    }


async def handle_code_ask(input: CodeAskInput) -> CodeAskOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    from server.embedding.embedder import get_embedder

    embedder = get_embedder()

    llm = None
    if input.synthesize:
        from server.llm.client import get_llm_client, get_llm_client_for_model
        from server.observability.budget import namespace_over_cap
        from server.settings import settings

        if not await namespace_over_cap(org, ns):
            override = settings.code_ask_llm_model
            llm = get_llm_client_for_model(override) if override else get_llm_client()

    abstain_threshold, noise_floor = await resolve_code_thresholds(org, ns)
    from server.code.app.answers import ask

    # The index head keys the negative cache; the block is the 30 s per-namespace one the
    # output carries anyway.
    as_of = await index_as_of(repo, org, ns)
    res = await ask(
        repo,
        org,
        ns,
        question=input.question,
        embedder=embedder,
        llm=llm,
        search_kwargs=_ask_search_kwargs(input, abstain_threshold, noise_floor),
        allow_synthesis=input.synthesize,
        no_cache=input.no_cache,
        index_head=str(as_of.get("commit") or ""),
    )
    record_code_ask_cache(
        cache=res.cache,
        namespace=ns,
        saved_ms=res.cache_saved_ms,
        saved_usd=res.cache_saved_usd,
    )

    # Cache-hit rate is the answer cache's whole value proposition; make it
    # visible per-request on the op span (bounded enum + float only).
    try:
        from opentelemetry import trace as _trace

        _span = _trace.get_current_span()
        if _span is not None and _span.is_recording():
            _span.set_attribute("loom.code.ask_source", res.source)
            _span.set_attribute("loom.code.ask_cache", cache_outcome_label(res.cache))
            _span.set_attribute("loom.code.cache_similarity", float(res.cache_similarity))
    except Exception:  # noqa: BLE001 — telemetry must not fail the request
        pass

    try:
        from server.observability.langfuse_scores import record_ask_scores

        record_ask_scores(
            source=res.source,
            coverage=res.coverage,
            retrieval_confidence=(res.retrieval.retrieval_confidence if res.retrieval else None),
            namespace=ns,
        )
    except Exception:  # noqa: BLE001 — telemetry must not fail the request
        pass

    trace_run_id = new_trace_run_id()
    ask_risk = res.retrieval.retrieval_risk if res.retrieval else "low"
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.ask",
        query=input.question,
        route=res.source,
        returned_ids=[c.get("unit_id", "") for c in res.citations],
        returned_paths=[c.get("path", "") for c in res.citations],
        returned_scores=[],
        top_k=len(res.citations),
        tokens_returned=res.tokens_returned,
        abstain=res.abstain,
        retrieval_risk=ask_risk,
        embedding_model=embedder.model_name,
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        # The cache outcome, and the served entry's address a negative code.outcome tombstones
        args={"cache": res.cache, "cache_entry": res.cache_entry}
        if res.cache_entry
        else {"cache": res.cache},
        as_of=as_of or None,
    )
    set_code_span_attributes(
        route=res.source,
        source=res.source,
        results=len(res.citations),
        tokens_returned=res.tokens_returned,
        abstain=res.abstain,
        retrieval_risk=ask_risk,
        consumer=input.consumer,
    )
    return CodeAskOutput(
        answer=res.answer,
        citations=res.citations,
        as_of=as_of,
        source=res.source,  # type: ignore[arg-type]
        cache_similarity=res.cache_similarity,
        cache=res.cache,
        cache_saved_ms=res.cache_saved_ms,
        cache_saved_usd=res.cache_saved_usd,
        abstain=res.abstain,
        abstain_reason=res.abstain_reason,
        retrieval_confidence=(res.retrieval.retrieval_confidence if res.retrieval else 0.0),
        tokens_returned=res.tokens_returned,
        context_tokens=(
            res.retrieval.tokens_returned if res.retrieval and res.source == "synthesized" else 0
        ),
        trace_run_id=trace_run_id,
    )
