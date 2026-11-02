"""The memory.search response: shaping the served hits, the output model, its
side channels (page handle, pending extraction, trace log, scratchpad), and
serving a parked page."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Literal, cast

from server.core.ch import get_client, sequential_reads
from server.embedding import get_embedder
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.evidence import _decorate_hits
from server.memory.app.search.knobs import _ground_relative_excerpts_enabled
from server.memory.app.search.payload import (
    _apply_bodies,
    _expand_source_bodies,
    _expand_unit_bodies,
    _fill_hit_tags,
    _fill_optional_hit_fields,
    _resolve_relative_words,
)
from server.memory.app.search.request import apply_explain_default
from server.memory.app.search.scope import _SearchRun
from server.memory.app.search.scoring import _score_explanation
from server.memory.app.search.telemetry import (
    _actual_work_payload,
    _disclose_and_expose,
    _schedule_trace_log,
)
from server.memory.app.search.tiers import (
    _apply_min_body_chars,
    _prepend_anchor,
    _prepend_recent,
)
from server.observability import degradations as _degradations

if TYPE_CHECKING:
    from server.memory.domain.retrieval_calibration import RetrievalCalibration

log = logging.getLogger(__package__)


async def _serve_parked_page(input: MemorySearchInput) -> MemorySearchOutput:
    """`page=<handle>` serves the next slice of a ranked tail an
    earlier call parked. No retrieval runs; the list is read by (org,
    namespace, blob_key) from loom.context_blobs, sliced at the handle's
    offset, and the response carries the handle for the slice after. An
    unknown or expired handle answers an empty page with `abstain_code=
    "page_expired"` — the caller re-runs the search, it does not retry."""
    from server.memory.app import search_pages as _pages

    parsed = _pages.parse_handle(input.page)
    embedder_name = ""
    try:
        embedder_name = str(getattr(get_embedder(), "model_name", "") or "")
    except Exception:  # noqa: BLE001
        pass
    empty = MemorySearchOutput(
        results=[],
        embedding_model=embedder_name,
        abstain=True,
        abstain_code="page_expired",
        abstain_reason=(
            "page handle is unknown, not yet flushed, or has expired; run the search again"
        ),
    )
    if parsed is None:
        return empty
    blob_key, offset = parsed
    # The handle was minted milliseconds ago by an insert that may not have reached this
    # replica yet: read it with sequential consistency, as the evidence readers do, or page
    # three of a quick walk can read "expired".
    from server.memory.store import read_your_writes_enabled

    _scope = sequential_reads() if read_your_writes_enabled() else nullcontext()
    with _scope:
        payload = await _pages.load_page(
            get_client(), org=input.org, namespace=input.namespace, blob_key=blob_key
        )
    if payload is None:
        return empty
    readable = await _readable_namespaces(input.org, input.namespace, payload)
    hits, nxt = _readable_slice(payload, offset, int(input.top_k), readable)
    # Hits from a namespace the caller lost are neither served nor counted; the
    # count is over the whole parked list, the same on every page while the
    # caller's readable namespaces do not change.
    withheld = sum(1 for h in payload.get("hits") or [] if h.get("namespace", "") not in readable)
    total = max(0, int(payload.get("candidates_total") or 0) - withheld)
    # A parked page was serialised without tags unless the first call asked; fill on this
    # call's own switch.
    await _fill_hit_tags(hits, input)
    apply_explain_default(hits, input)
    out = MemorySearchOutput(
        results=hits,
        embedding_model=embedder_name,
        candidates_total=total,
        namespaces_searched=[
            ns for ns in payload.get("namespaces_searched") or [input.namespace] if ns in readable
        ],
        namespaces_skipped=[ns for ns in payload.get("namespaces_skipped") or [] if ns in readable],
        page_offset=int(offset),
        next_page=_pages.make_handle(blob_key, nxt) if nxt >= 0 else "",
        abstain=not hits,
        abstain_code="" if hits else "no_match",
        abstain_reason="" if hits else "the parked list is exhausted",
        retrieval_risk="low" if hits else "abstain",
        score_explanation=(f"parked page served at offset {offset}; no retrieval ran"),
    )
    return out


async def _readable_namespaces(org: str, namespace: str, payload: dict[str, Any]) -> set[str]:
    """The namespaces of a parked page the caller may still read: the
    addressed one, and the others while they are in the caller's fan-out now.
    A page that names no other namespace skips the fan-out read."""
    from server.auth.fanout import resolve_fanout

    # Hits of the addressed namespace that carry no `namespace` (tier and
    # anchor rows) count as the addressed one.
    authorized = {"", namespace}
    named = {str(h.get("namespace", "")) for h in payload.get("hits") or []}
    named |= {
        *(payload.get("namespaces_searched") or []),
        *(payload.get("namespaces_skipped") or []),
    }
    if named <= authorized:
        return authorized
    try:
        mine = (await resolve_fanout(org)).namespaces
    except Exception as e:  # noqa: BLE001 — unresolved, the page narrows to the addressed namespace
        log.warning("parked page: fan-out unresolved for %s/%s: %s", org, namespace, e)
        return authorized
    return (named & set(mine)) | authorized


def _readable_slice(
    payload: dict[str, Any], offset: int, top_k: int, readable: set[str]
) -> tuple[list[_MemorySearchHit], int]:
    """Up to `top_k` readable hits from `offset` on, and the offset after the
    last row read (-1 at the end). Slices on past withheld rows, so a page is
    short only when the list is."""
    from server.memory.app import search_pages as _pages

    hits: list[_MemorySearchHit] = []
    nxt = offset
    while len(hits) < top_k and nxt >= 0:
        rows, nxt = _pages.slice_page(payload, offset=nxt, top_k=top_k - len(hits))
        for r in rows:
            if r.get("namespace", "") not in readable:
                continue
            try:
                hits.append(_MemorySearchHit.model_validate(r))
            except Exception:  # noqa: BLE001 — a row this build cannot read is skipped
                continue
        if not rows:
            break
    return hits, nxt


async def _shape_final_hits(
    ctx: SearchContext, final_hits: list[_MemorySearchHit]
) -> tuple[list[_MemorySearchHit], tuple[int, int, int], list[str]]:
    """Phase 22: shape the served hits — decorate, prepend recent/anchor
    rows, body floor, unit and source bodies, relative-date wording, foreign
    namespace marks, grounded excerpts, optional fields. Returns the hits,
    the unit-body counts and the namespaces searched."""
    input = ctx.input
    namespaces = ctx.namespaces
    actual_work = ctx.actual_work
    _clock = ctx.clock
    _decorate_hits(final_hits, input=input)
    final_hits = _apply_min_body_chars(
        await _prepend_anchor(await _prepend_recent(final_hits, input), input), input
    )
    _unit_bodies = await _expand_unit_bodies(
        final_hits, input, prefer_entities=list(ctx.effective_entity_filter)
    )
    _source_bodies = await _expand_source_bodies(
        final_hits, input, prefer_entities=list(ctx.effective_entity_filter)
    )
    if _source_bodies != (0, 0):
        ctx.actual_work["source_bodies"] = _source_bodies[0]
        ctx.actual_work["source_bodies_over_budget"] = _source_bodies[1]
    _apply_bodies(final_hits, input)
    _clock.mark("bodies")
    # A hit that still says "yesterday" beside its resolved event date is re-resolved by every
    # reader; state the date once, in the text, at read time. Applies to the excerpt every
    # consumer reads (the reader block, memory.ask's window, the wire).
    _resolve_relative_words(final_hits)

    # Say which namespaces were in scope (they already lead with the addressed one), and mark
    # every hit that came from a namespace other than the addressed one.
    _namespaces_searched = list(namespaces or [input.namespace])
    for _h in final_hits:
        _hns = str(getattr(_h, "namespace", "") or "")
        if _hns and _hns != input.namespace:
            _sc = dict(getattr(_h, "score_components", None) or {})
            _sc["foreign"] = 1.0
            _h.score_components = _sc

    # A fact excerpt that still carries a bare relative phrase ("… gala yesterday") gets the
    # date its row resolved it to appended, so no reader shifts the labelled date a second
    # time. Default on; a request or deployment can turn it off.
    if _ground_relative_excerpts_enabled(input):
        from server.memory.domain.reader_dates import ground_hits

        actual_work["relative_excerpts_grounded"] = ground_hits(final_hits)

    actual_work.update(await _fill_optional_hit_fields(final_hits, input, _namespaces_searched))
    return final_hits, _unit_bodies, _namespaces_searched


def _build_search_output(
    ctx: SearchContext,
    run: _SearchRun,
    final_hits: list[_MemorySearchHit],
    _namespaces_searched: list[str],
    _unit_bodies: tuple[int, int, int],
    derived_aggregate: dict[str, Any] | None,
    calibration: RetrievalCalibration,
    abstain_verdict: tuple[bool, str, str],
) -> MemorySearchOutput:
    """Phase 23: the main-path response — hits, verdict, plan, budget and
    work counters, plus the reader block when asked for."""
    from server.memory.app.retrieval import chunk_share as _chunk_share

    input = ctx.input
    embedder = ctx.embedder
    search_budget = ctx.search_budget
    search_plan = ctx.search_plan
    actual_work = ctx.actual_work
    _clock = ctx.clock
    abstain, abstain_code, abstain_reason = abstain_verdict
    _output = MemorySearchOutput(
        top_k_ceiling=run.top_k_ceiling,
        namespaces_searched=_namespaces_searched,
        namespaces_skipped=run.namespaces_skipped,
        results=final_hits,
        chunk_share=_chunk_share(final_hits, int(getattr(input, "top_k", 0) or len(final_hits))),
        bodies_returned=_unit_bodies[0],
        bodies_elided=_unit_bodies[1],
        tokens_returned=_unit_bodies[2],
        degraded=_degradations.current(),
        embedding_model=embedder.model_name,
        graph_seeds=run.graph_seeds,
        graph_neighbors_added=run.graph_neighbors,
        triples_extracted_from_query=len(ctx.query_triples),
        triples_matched=run.triples_matched,
        derived_aggregate=derived_aggregate,
        abstain=abstain,
        abstain_reason=abstain_reason,
        abstain_code=cast(
            Literal[
                "",
                "no_match",
                "below_threshold",
                "low_calibrated_confidence",
                "entity_absent",
                "page_expired",
            ],
            abstain_code,
        ),
        recipe=getattr(input, "recipe", "") or "",
        score_explanation=f"{_score_explanation(input)}; plan={search_plan.explain()}",
        retrieval_confidence=calibration.confidence,
        retrieval_risk=cast(
            Literal["low", "medium", "high", "abstain"],
            calibration.risk,
        ),
        route_agreement=calibration.route_agreement,
        calibration_explanation=calibration.explanation,
        fallback_rounds_used=search_budget.rounds_used,
        fallback_budget_exhausted=search_budget.exhausted,
        fallback_elapsed_ms=search_budget.elapsed_ms(),
        retrieval_plan=search_plan.explain(),
        retrieval_cost_estimate=run.retrieval_cost_estimate,
        retrieval_work=_actual_work_payload(actual_work, search_budget, clock=_clock),
    )
    if getattr(input, "render", "") == "reader":
        from server.memory.app.ask.reader_render import reader_instructions, render_reader_block

        _output.reader_block = render_reader_block(final_hits, input.query)
        _output.reader_instructions = reader_instructions()
    _output.set_expansion_phrases = len(ctx.set_expansion_phrases)
    return _output


async def _finish_search_output(
    ctx: SearchContext, _output: MemorySearchOutput, final_hits: list[_MemorySearchHit]
) -> None:
    """Phase 24: the response's side channels — candidate count and optional
    page handle, pending-extraction count, trace log, disclosure/exposure,
    the run hint and the scratchpad."""
    input = ctx.input
    embedder = ctx.embedder
    search_budget = ctx.search_budget
    # The count is free and always on; the handle costs one small insert and is made only when
    # asked for and only when there is a tail to serve.
    _output.candidates_total = max(int(ctx.candidates_total), len(final_hits))
    # What a caller re-renders the reader block with, or credits a merged pass with.
    _output._settled_input = input
    if getattr(input, "_merged_into_parent", False):
        # Only this pass's hits reach the caller, through the parent that merges them: the
        # page, the trace log, the exposure record and the hints are the parent's.
        return
    if getattr(input, "paging", False) and ctx.page_tail:
        try:
            from server.memory.app import search_pages as _pages
            from server.settings import settings as _pg_settings

            _content = _pages.encode_page(
                query=input.query,
                top_k=int(input.top_k),
                candidates_total=_output.candidates_total,
                tail=ctx.page_tail,
                namespaces_searched=_output.namespaces_searched,
                namespaces_skipped=_output.namespaces_skipped,
            )
            _blob_key = await _pages.store_page(
                get_client(),
                org=input.org,
                namespace=input.namespace,
                agent=str(getattr(input, "agent", "*") or "*"),
                user_id=str(getattr(input, "user_id", "-") or "-"),
                session_id=str(getattr(input, "session_id", "-") or "-"),
                run_id=str(getattr(input, "run_id", "-") or "-"),
                content=_content,
                ttl_s=int(_pg_settings.memory_search_page_ttl_s),
            )
            _output.next_page = _pages.make_handle(_blob_key, 0)
            ctx.actual_work["page_tail_rows"] = len(ctx.page_tail)
        except Exception as e:  # noqa: BLE001 — a page handle is a courtesy, never a failure
            log.warning("memory.search page handle not stored (fail-open): %s", e)
    try:
        from server.memory.app.pending_disclosure import pending_extraction as _pending

        _output.pending_extraction = await _pending(
            org=input.org,
            namespace=input.namespace,
            agent=getattr(input, "agent", "*"),
            user_id=getattr(input, "user_id", None),
        )
    except Exception:  # noqa: BLE001
        _output.pending_extraction = 0
    _schedule_trace_log(
        input, _output, embedder, search_budget.elapsed_ms(), query_vec=ctx.query_vec
    )
    _disclose_and_expose(ctx, _output)
    if _output.results:
        from server.memory.api.models_common import run_next_hint

        _output.next = run_next_hint(input.run_id)
    await _attach_scratchpad(input, _output)


async def _attach_scratchpad(input: Any, output: Any) -> None:
    """The identity's working-memory blocks ride the response when the call named a session or
    a run. Fail-open: a read error leaves the field empty, never the search failed."""
    if not (getattr(input, "session_id", None) or getattr(input, "run_id", None)):
        return
    try:
        from server.memory.app import scratchpad as _sp
        from server.settings import settings as _s

        blocks = await _sp.load(
            input.org,
            input.namespace,
            getattr(input, "agent", ""),
            getattr(input, "user_id", None),
            getattr(input, "session_id", None),
            getattr(input, "run_id", None),
            max_chars=int(_s.memory_scratchpad_max_chars),
        )
    except Exception as e:  # noqa: BLE001 — a missing note must not fail a search
        log.debug("scratchpad load skipped: %s", e)
        return
    if blocks is None:
        return
    output.scratchpad = blocks.rendered
    output.scratchpad_blocks = blocks.block_count
