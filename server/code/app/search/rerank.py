"""The rerank seam: an optional cross-encoder pass over the fused head,
budgeted and fail-open."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.app.spans import code_span
from server.code.domain.units import CodeUnit
from server.core.llm_free import reads_llm_free
from server.settings import settings

logger = logging.getLogger(__name__)


async def _rerank_stage(
    rerank_endpoint: str,
    query: str,
    ranked: list[CodeUnit],
    work: dict[str, Any],
    result: Any,
    *,
    prose: bool,
) -> list[CodeUnit]:
    """The rerank seam: the request's endpoint over the top 50, or the deployment's
    kind-gated default over its own top and excerpt size when the request names none and
    the question is prose; "none" turns both off. Fail-open; the fused order is kept on
    any failure."""
    if rerank_endpoint == "none":
        return ranked
    top, chars = 50, 300
    if not rerank_endpoint and ranked and prose:
        rerank_endpoint, top, chars = _default_rerank(work)
    if not rerank_endpoint or not ranked:
        return ranked
    # under a budget: skipped when what is left cannot pay for it, cut at what is left
    budget = result.budget
    left_ms = None if budget is None else budget.left_ms() - _PAGE_RESERVE_MS
    if budget is not None and left_ms is not None and left_ms < _RERANK_MIN_MS:
        budget.drop("rerank skipped")
        return ranked
    with code_span("search.rerank", results=min(len(ranked), top)):
        try:
            async with asyncio.timeout(None if left_ms is None else left_ms / 1000.0):
                ranked, reranked = await _rerank_units(
                    rerank_endpoint,
                    query,
                    ranked,
                    top=top,
                    excerpt_chars=chars,
                )
        except TimeoutError:
            if budget is not None:
                budget.drop("rerank cut at the deadline")
            return ranked
    if reranked:
        # only an order the model produced is named, beside what the explanation holds
        work["inline_rerank_used"] = 1
        extra = "rerank=" + rerank_endpoint.split(":")[0]
        prior = result.score_explanation_extra
        result.score_explanation_extra = f"{prior}, {extra}" if prior else extra
    return ranked


# Under a request budget: what the page after the rerank keeps back, and the least a
# rerank call is started with.
_PAGE_RESERVE_MS = 100


_RERANK_MIN_MS = 500


def _default_rerank(work: dict[str, Any]) -> tuple[str, int, int]:
    """(endpoint, top, excerpt_chars) of the deployment's default rerank for this
    question's kind, or ("", 0, 0) when none applies or reads are LLM-free (a reranker
    is a model call, and such a read runs only the one its request names)."""
    from server.code.domain.index.question_kind import kind_allowed

    endpoint = str(settings.code_search_rerank_default).strip()
    if (
        not endpoint
        or reads_llm_free()
        or not kind_allowed(
            str(work.get("question_kind") or "other"),
            settings.code_search_rerank_kinds,
        )
    ):
        return "", 0, 0
    work["rerank_default"] = 1
    return (
        endpoint,
        max(1, int(settings.code_search_rerank_top)),
        max(60, int(settings.code_search_rerank_excerpt_chars)),
    )


async def _rerank_units(
    endpoint: str,
    query: str,
    units: list[CodeUnit],
    *,
    top: int = 50,
    excerpt_chars: int = 300,
) -> tuple[list[CodeUnit], bool]:
    """Re-order the top-`top` units via the shared rerank service (fail-open).
    Returns (units, reranked): False when the call failed and the fused
    order was kept, so the caller can count only real rerank assists."""
    from server.memory.app.rerank import RerankCandidate, rerank

    head, tail = units[:top], units[top:]
    by_id = {u.unit_id: u for u in head}
    candidates = [
        RerankCandidate(
            memory_key=u.unit_id,
            namespace="",
            # what a cross-encoder needs: where it lives + signature + body head
            content_excerpt=(
                f"{u.path} {u.symbol}{u.signature}\n{u.body[: max(600, excerpt_chars)]}"
            ),
            memory_type=u.kind,
            similarity=u.score,
        )
        for u in head
    ]
    try:
        ordered = await rerank(
            endpoint=endpoint,
            prompt_template="",
            query=query,
            candidates=candidates,
            excerpt_chars=excerpt_chars,
        )
    except Exception as e:  # noqa: BLE001 — fail-open keeps fused order
        logger.warning("code.search rerank failed (%s); fused order kept", e)
        return units, False
    # The service fails open by handing back the SAME candidate list (a provider error, or a reply
    # it could not parse — ~1.7% of calls). That is not a rerank: report it as none, so
    # `inline_rerank_used` counts only orders the model actually produced.
    if ordered is candidates:
        return units, False
    reordered = [by_id[c.memory_key] for c in ordered if c.memory_key in by_id]
    seen = {u.unit_id for u in reordered}
    reordered += [u for u in head if u.unit_id not in seen]
    return reordered + tail, True
