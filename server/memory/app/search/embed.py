"""The query embedding: what text is embedded, optional HyDE, and the bounded embed."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from server.core.errors import EmbedTimeoutError
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.budget import SearchBudget
from server.memory.app.search.query_plan import _is_fast_posture
from server.memory.app.search.query_spelling import embed_query
from server.memory.domain.search_prompts import _HYDE_PROMPT
from server.observability.genai import llm_stage

if TYPE_CHECKING:
    from server.embedding import Embedder
    from server.memory.app.search.clock import StageClock

log = logging.getLogger(__package__)


async def _bounded_query_embed(
    embedder: Any, text: str, actual_work: dict[str, Any] | None = None
) -> list[float]:
    """The query vector, within `memory_search_embed_bound_s` per attempt.

    The provider client allows 20 s per request and three retries, which is right for a write
    and wrong for a read the agent is waiting on (a single slow embed can take nearly the whole
    search's wall time). A timed-out attempt is cancelled (the request with it) and retried up
    to `memory_search_embed_bound_retries` times; then the search answers 503 `EMBED_TIMEOUT`
    so the caller retries instead of waiting. A bound of 0 keeps the unbounded call."""
    from server.settings import settings as _s

    bound = float(_s.memory_search_embed_bound_s)
    if bound <= 0:
        unbounded: list[float] = await embedder.embed(text)
        return unbounded
    attempts = 1 + max(0, int(_s.memory_search_embed_bound_retries))
    for attempt in range(1, attempts + 1):
        try:
            vec: list[float] = await asyncio.wait_for(embedder.embed(text), timeout=bound)
            return vec
        except TimeoutError:
            if actual_work is not None:
                actual_work["embed_timeouts"] = int(actual_work.get("embed_timeouts", 0) or 0) + 1
            log.warning("query embed exceeded %.1fs (attempt %d/%d)", bound, attempt, attempts)
    raise EmbedTimeoutError(
        f"the embedding provider did not answer within {bound:.1f}s on "
        f"{attempts} attempt(s); the search wrote nothing"
    )


def _embed_text_and_window(
    input: MemorySearchInput,
    resolved_dates: list[str],
    obs_date: str | None,
    actual_work: dict[str, Any],
) -> tuple[str, str, str]:
    """Phase 8: what gets embedded. Returns the resolved-date suffix, the
    text to embed (query + suffix, until HyDE replaces it) and the point
    anchor date; records the question's time window as telemetry."""
    # Resolved ISO dates are appended to whatever we embed so the embedder
    # sees vocabulary overlapping date-stamped memory content. Kept as a
    # reusable suffix so the HyDE branch re-applies it instead of dropping
    # the dates when it overwrites hyde_query.
    date_suffix = (" " + " ".join(resolved_dates)) if resolved_dates else ""
    hyde_query = input.query + date_suffix
    query_anchor_iso = resolved_dates[0] if resolved_dates else ""
    # The window the question is about (start, end, kind), deterministic; reported for
    # telemetry, the ranking keeps the point anchor.
    try:
        from server.memory.domain.question_window import resolve_time_window as _rtw

        _qwin = _rtw(input.query, obs_date)
        if _qwin is not None:
            actual_work["question_window"] = (
                f"{_qwin.kind}:{_qwin.start or '*'}..{_qwin.end or '*'}"
            )
    except Exception as e:  # noqa: BLE001 — a window is telemetry, never a failure
        log.debug("question window skipped: %s", e)
    return date_suffix, hyde_query, query_anchor_iso


async def _embed_search_query(
    input: MemorySearchInput,
    embedder: Embedder,
    hyde_query: str,
    date_suffix: str,
    search_budget: SearchBudget,
    actual_work: dict[str, Any],
    _clock: StageClock,
) -> list[float]:
    """Phase 10: the one query embedding. When the plan turned HyDE on, a
    bounded hypothetical answer joins the embedded text (fail-open to the
    bare query). Returns the query vector."""
    from server.observability.otel import traced

    # HyDE is the planner's call, made before the first pass; nothing re-spends it after.
    # The fast posture makes no LLM call, whichever field turned HyDE on.
    hyde_active = bool(input.hyde) and not _is_fast_posture(input)
    if hyde_active:
        actual_work["hyde_initial_used"] = True
        actual_work["llm_hyde_calls"] = 1
        search_budget.note_llm_call(1)
        try:
            from server.llm.client import get_llm_client

            llm = get_llm_client()
            # Bounded: HyDE generation rides the search's critical path,
            # so a slow provider must degrade to the bare query embed
            # (TimeoutError lands in the except below) instead of
            # stalling the search for the transport's full retry window.
            # Label the leg so the generation span this produces is named
            # `llm.hyde` rather than after the model. HyDE is called inline
            # here, not through a search_llm helper, so it needs its own.
            with llm_stage("hyde"):
                hyde_result = await asyncio.wait_for(
                    llm.complete(
                        system=_HYDE_PROMPT,
                        user=f"Question: {input.query}\n\nHypothetical answer:",
                        max_tokens=128,
                    ),
                    timeout=6.0,
                )
            hyde_text = (hyde_result.text or "").strip()
            if hyde_text:
                # Embed query + hypothetical: the ANN gets both the
                # question's lexical signal and the memory-shaped signal.
                # Re-append the date suffix so dates survive the overwrite.
                hyde_query = f"{input.query}\n{hyde_text}{date_suffix}"
        except Exception as e:  # noqa: BLE001 — HyDE is optional; the plain query embed still runs
            log.warning("HyDE generation failed (non-fatal): %s", e)

    async def embed(text: str) -> list[float]:
        actual_work["embedding_calls"] = int(actual_work["embedding_calls"]) + 1
        return await _bounded_query_embed(embedder, text, actual_work)

    with traced("embed.compute.query", model=embedder.model_name):
        _clock.mark("plan")
        query_vec, spellings = await embed_query(embed, hyde_query, input, embedder.model_name)
        _clock.mark("embed")
    if spellings:
        actual_work["query_spellings"] = spellings
    elif spellings is None:
        actual_work["query_spellings_skipped"] = True
    return query_vec
