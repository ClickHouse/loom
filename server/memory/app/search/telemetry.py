"""What a search records about itself: the retrieval_work ledger and its stage
spans, the search_trace_log row and query node, the exposure ledger, and the
auto-touch recall credit. Every write here is fire-and-forget; none fails a read."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput
from server.memory.app import after_write
from server.memory.app.search.budget import SearchBudget
from server.memory.app.search.context import SearchContext

if TYPE_CHECKING:
    from server.memory.app.search.clock import StageClock

log = logging.getLogger(__package__)

# Fields that are already their own trace columns, or are the query itself.
_SHAPE_EXCLUDE = frozenset({"query", "org", "namespace", "agent", "run_id"})


def search_shape_sha256(input: MemorySearchInput) -> str:
    """Digest of everything that defines a search except its text and identity.

    Two trace rows with the same `query` and the same shape are the same request; two with
    different shapes are two searches sharing a sentence. Benches sweep search_mode and tags over
    one question, so most same-text pairs are the latter, and pairing on query text alone would
    report large "non-determinism" that is not there. Non-default fields only, so a client that
    omits a default and one that sends it hash alike; sorted keys, so field order does not
    matter. Carries no user text: it is a hash, and the fields it covers are modes, limits, flags
    and filters."""
    try:
        shape = input.model_dump(exclude=set(_SHAPE_EXCLUDE), exclude_defaults=True)
    except Exception:  # noqa: BLE001 — a row without a shape beats no row
        return ""
    canonical = json.dumps(shape, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reader_actor() -> str:
    from server.memory.app.provenance import writer_actor

    return writer_actor()


def _build_trace_row(
    input: MemorySearchInput, output: Any, embedder: Any, latency_ms: int
) -> dict[str, Any]:
    """Pure builder for one loom.search_trace_log row — extracted so it can be
    unit-tested without the async / ClickHouse plumbing.

    The `query` column is the one field here carrying free user text, so it is
    gated on the namespace's capture consent — the same answer that decides
    whether prompt text reaches Langfuse, not a second notion of it. When
    consent is `off` the row is still written with the query blanked: recall,
    abstain rate and latency carry no user content, and dropping the row
    entirely would make a privacy choice also a quality-blindness choice, which
    is what pushes operators into refusing the privacy setting.
    """
    hits = list(getattr(output, "results", None) or [])
    try:
        from server.observability.genai import current_capture_level

        may_log_query = current_capture_level() != "off"
    except Exception:  # noqa: BLE001 — fail closed: no consent answer, no query
        may_log_query = False
    return {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        # The reader, server-stamped (never the body's `agent`): memory.search_trace_log
        # shows a non-admin only the rows recorded for it.
        "actor": _reader_actor(),
        "query": input.query if may_log_query else "",
        "returned_keys": [h.memory_key for h in hits],
        "returned_scores": [float(getattr(h, "rank_score", 0.0)) for h in hits],
        "top_k": int(input.top_k),
        "result_count": len(hits),
        "abstain": 1 if getattr(output, "abstain", False) else 0,
        "retrieval_risk": str(getattr(output, "retrieval_risk", "") or ""),
        "embedding_model": getattr(embedder, "model_name", ""),
        "latency_ms": int(latency_ms),
        # A read labels its run through the transport (the SDK's ambient run / the plugin's
        # `X-Loom-Run-Id`) because a body `run_id` on a read is a FILTER. The ledger row already
        # takes that label; the trace row does too, so run.get's "recalled" counts the searches
        # that ran under the run.
        "run_id": input.run_id or _ledger_run_label() or "-",
        "shape_sha256": search_shape_sha256(input),
        # The stage partition and the loop-lag sample the response already carries land beside
        # latency_ms, so "which stage was slow" is answerable from the table, not only the wire.
        "stage_ms": _stage_ms_column(getattr(output, "retrieval_work", None)),
        "loop_lag_ms": _loop_lag_column(getattr(output, "retrieval_work", None)),
        # The op the caller invoked (memory.ask, context.pack, the MCP search tool, or
        # memory.search itself), so a row can say whether its wording is what a person typed or
        # what a caller composed.
        "operation": _trace_operation(),
    }


def _ledger_run_label() -> str:
    """The run the transport bound this request to ('' outside one)."""
    try:
        from server.observability.run_context import current_ledger_run

        return current_ledger_run()
    except Exception:  # noqa: BLE001 — telemetry never fails a read
        return ""


def _trace_operation() -> str:
    """The wire op stamped on this request, "memory.search" when the stamp is
    missing (a worker or a test). Same source the exposure ledger labels with."""
    try:
        from server.observability.genai import current_operation

        return current_operation() or "memory.search"
    except Exception:  # noqa: BLE001 — telemetry never fails a read
        return "memory.search"


def _stage_ms_column(work: Any) -> dict[str, int]:
    """`retrieval_work.stage_ms` as the Map(String, UInt32) column: whole
    milliseconds per stage, `total` included so the partition can be checked
    in SQL. Odd shapes yield an empty map — telemetry never fails a read."""
    stages = work.get("stage_ms") if isinstance(work, dict) else None
    if not isinstance(stages, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in stages.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[str(k)] = int(round(float(v)))
    return out


def _loop_lag_column(work: Any) -> float:
    lag = work.get("loop_lag_ms") if isinstance(work, dict) else None
    if isinstance(lag, (int, float)) and not isinstance(lag, bool) and lag >= 0:
        return float(lag)
    return 0.0


def _build_query_node_row(
    input: MemorySearchInput, output: Any, embedder: Any, query_vec: list[float] | None
) -> dict[str, Any] | None:
    """Pure builder for one loom.query_nodes row, or None.

    Gated on the same capture consent as the trace row's `query` column —
    the node IS the query text — and empty when the search returned nothing
    (a node with no answer set is no witness for a later query)."""
    if not query_vec:
        return None
    try:
        from server.observability.genai import current_capture_level

        if current_capture_level() == "off":
            return None
    except Exception:  # noqa: BLE001 — fail closed: no consent answer, no node
        return None
    from server.memory.domain.query_nodes import build_query_node_row

    hits = list(getattr(output, "results", None) or [])
    return build_query_node_row(
        org=input.org,
        namespace=input.namespace,
        query=input.query,
        query_vec=list(query_vec),
        embedding_model=str(getattr(embedder, "model_name", "") or ""),
        returned_keys=[h.memory_key for h in hits],
        abstain=bool(getattr(output, "abstain", False)),
        session_id=str(input.session_id or "-"),
    )


def _schedule_exposure(input: MemorySearchInput, hits: list[Any]) -> None:
    """Append the returned keys to the exposure ledger, labelled with the op the caller invoked
    (memory.ask and context.pack route through this handler). Fire-and-forget; never on the
    read path's critical section."""
    try:
        from server.memory.app.exposure_ledger import keys_of, record_exposure
        from server.observability.genai import current_operation

        keys, below = keys_of(hits)
        record_exposure(
            operation=current_operation() or "memory.search",
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            run_id=input.run_id,
            read_scope=str(input.read_scope or "namespace"),
            query=input.query,
            top_k=int(input.top_k or 0),
            keys=keys,
            below_floor=below,
        )
    except Exception as e:  # noqa: BLE001 — telemetry never fails a read
        log.warning("exposure-ledger schedule failed (non-fatal): %s", e)


def _schedule_trace_log(
    input: MemorySearchInput,
    output: Any,
    embedder: Any,
    latency_ms: int,
    query_vec: list[float] | None = None,
) -> None:
    """Append one telemetry row per search to loom.search_trace_log, when
    LOOM_SEARCH_TRACE_LOG_ENABLED, and one query node to loom.query_nodes, when
    LOOM_MEMORY_QUERY_NODES_ENABLED. Fire-and-forget: neither is read on this request's path,
    so a failed write is logged and swallowed."""
    from server.settings import settings as _s

    writes: list[tuple[str, dict[str, Any]]] = []
    if _s.search_trace_log_enabled:
        writes.append(
            ("loom.search_trace_log", _build_trace_row(input, output, embedder, latency_ms))
        )
    if _s.memory_query_nodes_enabled:
        node = _build_query_node_row(input, output, embedder, query_vec)
        if node is not None:
            writes.append(("loom.query_nodes", node))
    if not writes:
        return

    async def _bg() -> None:
        for table, row in writes:
            try:
                await get_client().insert(
                    table,
                    [row],
                    settings={"async_insert": 1, "wait_for_async_insert": 0},
                )
            except Exception as e:  # noqa: BLE001
                log.warning("%s write failed (non-fatal): %s", table, e)

    after_write.schedule(_bg(), name="memory.search.trace_log")


def _actual_work_payload(
    work: dict[str, Any], budget: SearchBudget, *, clock: Any = None
) -> dict[str, Any]:
    if clock is not None:
        clock.finish()
    out = dict(work)
    out["fallback_llm_calls_used"] = int(budget.llm_calls_used)
    out["fallback_vector_queries_used"] = int(budget.vector_queries_used)
    out["fallback_llm_call_budget"] = int(budget.max_llm_calls)
    out["fallback_vector_query_budget"] = int(budget.max_vector_queries)
    # Cascade-wide: every LLM call issued (plan + fallback), not the
    # fallback-only counter above — this is the capacity-sizing number.
    out["llm_calls_total"] = int(budget.llm_calls_metered)
    return out


async def _staged(ctx: SearchContext, name: str, coro: Awaitable[None], *, active: bool) -> None:
    """Await one additive pool stage under a child span `search.<name>`.

    The sub-query, set-expansion, anchor, key-match and digest-descent arms each issue their own
    ClickHouse statements; without a span per stage, over half of a benchmark search's root
    span (~600 of 1,150 ms) reads as unattributed in the waterfall. The span carries what the
    stage added to the pool (`loom.search.added`) and the pool size after it, and the same count
    lands in `retrieval_work.<name>_added`. A stage that will not run (`active` false) is
    awaited without a span, so a search does not grow five empty spans. Never raises on the
    telemetry side."""
    if not active:
        await coro
        return
    from server.observability.otel import traced

    before = len(ctx.all_hits)
    with traced(f"search.{name}", **{"loom.search.namespaces": len(ctx.namespaces)}) as span:
        try:
            await coro
        finally:
            added = len(ctx.all_hits) - before
            ctx.actual_work[f"{name}_added"] = added
            try:
                if span is not None and span.is_recording():
                    span.set_attribute("loom.search.added", added)
                    span.set_attribute("loom.search.pool", len(ctx.all_hits))
            except Exception:  # noqa: BLE001 — telemetry never fails a read
                pass


async def _recall_credit_decision(input: MemorySearchInput) -> Any:
    """Is this read's recall credit written? The labels, the
    named consumer and the run come from the request; the closer window is
    one cached ledger read per (org, consumer)."""
    from server.memory.app import recall_credit
    from server.observability.consumer import current_consumer, current_consumer_source
    from server.observability.run_context import ledger_run_for

    return await recall_credit.decide_recall_credit(
        org=input.org,
        agent=input.agent,
        consumer=current_consumer(),
        consumer_source=current_consumer_source(),
        run_id=ledger_run_for(input.run_id),
        session_id=input.session_id,
    )


def _disclose_and_expose(ctx: SearchContext, output: MemorySearchOutput) -> None:
    """The served response carries the recall-credit verdict (None = absent from the wire) and
    lands in the exposure ledger."""
    output.reinforcement = ctx.recall_credit
    _schedule_exposure(ctx.input, list(output.results))


# The ops that return memory to an agent: what a `served` event counts. The rest of the ops
# that run a search (decision.precedents, memory.apply, the workers' passes) filter, fetch
# past what they return, or select rows to change, and record none.
SERVED_OPERATIONS = frozenset(
    {"memory.search", "search.query", "context.pack", "memory.ask", "procedure.match"}
)
# Of those, the ops whose caller sets `auto_touch` on the search itself: their
# `auto_touch: false` is the caller's "record no usage". context.pack, memory.ask and
# procedure.match turn their inner pass's auto_touch off to keep the ranking credit out,
# and still serve what the pass finds.
_CALLER_TOUCH_OPERATIONS = frozenset({"memory.search", "search.query"})


def _served_agent(input: MemorySearchInput) -> str | None:
    """The agent a `served` event names, or None when this pass records none: not for an op
    that does not return memory to an agent, the dashboard or the assistant (a person
    inspecting), a bench label on the consumer or the calling agent, or a caller that asked
    for no usage. A context pack or an ask searches as `*`; the event names the agent that
    called it."""
    from server.memory.app import recall_credit
    from server.observability.consumer import INSPECTOR_CONSUMERS
    from server.observability.genai import current_operation, request_identity

    operation = current_operation()
    if operation not in SERVED_OPERATIONS:
        return None
    if operation in _CALLER_TOUCH_OPERATIONS and not input.auto_touch:
        return None
    consumer = recall_credit.current_named_consumer()
    if consumer in INSPECTOR_CONSUMERS or recall_credit.is_bench_label(consumer):
        return None
    agent = str(input.agent or "")
    if recall_credit.is_agent_sentinel(agent):
        agent = request_identity().get("agent") or agent
    return None if recall_credit.is_bench_label(agent) else agent


def _not_yet_served(input: MemorySearchInput, hits: list[Any]) -> list[Any]:
    """The hits this request has not written a `served` event for yet: memory.ask runs
    several passes whose pools overlap, and one ask is one read."""
    from server.core.request_tail import first_in_request

    return [
        h
        for h in hits
        if first_in_request(("served", input.org, h.namespace or input.namespace, h.memory_key))
    ]


async def _record_usage(input: MemorySearchInput, hits: list[Any]) -> str | None:
    """The usage a served pass writes: a `recall` credit when the reader is credited, else a
    `served` event when `_served_agent` names one. Returns the credit verdict for the
    response (None when auto_touch was off)."""
    disclosure: str | None = None
    if input.auto_touch:
        decision = await _recall_credit_decision(input)
        disclosure = str(decision.disclosure)
        if decision.credited:
            _schedule_usage_events(input, hits, "recall")
            return disclosure
    agent = _served_agent(input)
    if agent is not None:
        from server.memory.store.usage import SERVED_EVENT

        fresh = _not_yet_served(input, hits)
        if fresh:
            _schedule_usage_events(input, fresh, SERVED_EVENT, agent=agent)
    return disclosure


async def _schedule_auto_touch(ctx: SearchContext) -> None:
    """Auto-touch. Record usage as append-only events; `memory_usage_totals_mv` derives
    recall_count / last_recalled_at from the `recall` credits without mutating `loom.memory` on
    the read path. Only a reader that closes the loop is credited; the verdict rides the
    response as `reinforcement`. Snapshot final_hits since later fallbacks may reassign it."""
    if ctx.final_hits:
        ctx.recall_credit = await _record_usage(ctx.input, list(ctx.final_hits))


def _schedule_usage_events(
    input: MemorySearchInput, hits: list[Any], event_type: str, *, agent: str | None = None
) -> None:
    """One usage event per returned key, written in the background: observability only,
    never read by retrieval, so fire-and-forget rather than block on a memory_id lookup + N
    writes. The request's consumer is the event's audit column."""
    from server.observability.consumer import current_consumer

    _consumer = str(current_consumer() or "")
    _agent = input.agent if agent is None else agent

    async def _bg_usage() -> None:
        try:
            from server.memory.store import usage as memory_repo_usage

            ns_keys: dict[str, list[str]] = {}
            for h in hits:
                ns_keys.setdefault(h.namespace or input.namespace, []).append(h.memory_key)
            # Through the same funnel memory.ask's cited-used credit takes, rather than one
            # per row (which would credit every session's copy of a key).
            await memory_repo_usage.record_usage_for_keys(
                input.org, ns_keys, event_type=event_type, agent=_agent, consumer=_consumer
            )
        except Exception as e:  # noqa: BLE001 — background usage events must not fail the search
            log.warning("%s-event log failed (non-fatal): %s", event_type, e)

    after_write.schedule(_bg_usage(), name=f"memory.search.{event_type}")


async def credit_merged_hits(input: MemorySearchInput, hits: list[Any]) -> None:
    """Exposure and usage for the hits of a pass merged into another response
    (`_merged_into_parent`), which writes neither itself: `hits` are the ones the parent
    merged, and `input` is the pass's settled input (`_settled_input`), with the window the
    parent asked for."""
    _schedule_exposure(input, hits)
    if hits:
        await _record_usage(input, hits)


async def _start_actual_work(input: MemorySearchInput) -> tuple[dict[str, Any], StageClock]:
    """Phase 3: open the work ledger. Seeds the retrieval_work counters, the
    loop-lag sample and the stage clock, and flags an enumeration-shaped query.
    Returns the counters dict (mutated by every later phase) and the clock."""
    actual_work: dict[str, Any] = {
        "query_enrichment_used": False,
        "hyde_initial_used": False,
        "llm_hyde_calls": 0,
        "llm_graph_triple_calls": 0,
        "llm_iterative_judge_calls": 0,
        "embedding_calls": 0,
        "vector_queries": 0,
        "entity_ann_queries": 0,
        "sub_query_count": 0,
        "namespace_count": 0,
        "pool_top_k": 0,
        "candidates_before_final": 0,
        "external_rerank_used": False,
        "intent_parser_used": False,
    }
    # Per-stage wall time + one event-loop lag sample ride retrieval_work, so a slow search
    # names its stage and a saturated worker is not misread as a slow database.
    from server.memory.app.search.clock import StageClock, sample_loop_lag_ms

    actual_work["loop_lag_ms"] = await sample_loop_lag_ms()
    _clock = StageClock(actual_work)
    # Extraction-shaped query: the query asks for the stored records as a class (a bulk-
    # extraction probe). Flagged on the diagnostic payload and counted; never refused —
    # refusal rules are bypassed by harmless phrasings, the signal is what a breadth budget
    # can gate on.
    try:
        from server.memory.domain.prompt_fence import enumeration_shaped

        if enumeration_shaped(input.query):
            actual_work["enumeration_shaped"] = True
            from server.observability.metrics import record_enumeration_shaped

            record_enumeration_shaped(namespace=input.namespace)
    except Exception as e:  # noqa: BLE001 — a signal never fails a read
        log.debug("enumeration-shape check failed: %s", e)

    return actual_work, _clock
