"""Domain metrics for Loom — retrieval quality and system state.

`otel.py` owns the OTel plumbing (providers, exporters, the per-op
`loom.requests` / `loom.request.duration` pair). This module owns the metrics
that describe *whether Loom is doing its job*, which is a different question
from whether requests are fast.

Rate, latency and errors per operation say nothing about whether retrieval
found anything, what it cost, or whether the background planes keep up: a
search that returns confidently wrong results in 40ms looks perfect on an HTTP
dashboard.

Two design rules, both load-bearing:

1. **Cardinality discipline.** Attributes are drawn only from a closed set:
   operation, namespace, and bounded enums (plane names, abstain reasons,
   outcomes). Never `memory_key`, `user_id`, `session_id`, query text, or any
   id — those are unbounded and would turn `otel_metrics_*` into a
   write-amplification engine. A value a caller can choose goes through a
   bounding helper (`_clamp_ns`, `messages_bucket`) first.

2. **State goes in observable gauges, not counters.** Backlogs, breaker state
   and row counts are *levels*, not events. OTel observable gauges invoke a
   callback at export time, so they cost nothing on the request path — but the
   export interval is 3s and the export timeout is 2.5s, so any callback that
   touches ClickHouse MUST be cached AND must never produce inline (see
   `metrics_gauges._TTLCache`). An uncached CH query here would mean 20 extra
   queries a minute forever; a blocking one means a `MetricsTimeoutError` and a
   dropped export cycle every time the cache refreshes. The gauges live in
   `metrics_gauges`; this module holds the event instruments.
"""

from __future__ import annotations

import logging
from typing import Any

from server.settings import settings as _settings

log = logging.getLogger(__name__)

# Instruments; None until `init_domain_metrics()` runs (or if OTel is off).
_search_results: Any = None
_search_top_similarity: Any = None
_search_confidence: Any = None
_search_judged_relevance: Any = None
_search_judge_verdict: Any = None
_search_abstain: Any = None
_search_plane_hits: Any = None
_search_assist: Any = None
_memory_writes: Any = None
_paid_errors: Any = None
_memory_build_tokens: Any = None
_memory_extraction_yield: Any = None
_extract_cost_per_row: Any = None
_memory_drops: Any = None
_enumeration_shaped: Any = None
_attribute_tagged: Any = None
_policy_decisions: Any = None
_procedure_runs: Any = None
_procedure_evolve: Any = None
_op_cost: Any = None
_op_tokens: Any = None
_llm_fallback: Any = None
_tracker_sections: Any = None
_tracker_section_ms: Any = None
_llm_response_cache: Any = None
_worker_passes: Any = None
_worker_pass_duration: Any = None
_auth_refusals: Any = None
_agents_provisioned: Any = None

_initialized = False

# Planes whose `distance` is a genuine vector cosine. Everything else stores a
# synthesized value: "record" holds 1 - lexical_rank_score, and
# "computed"/"derived"/"scalar" evidence rows hardcode distance=0.0. Only these
# may feed a similarity metric — see record_search_quality.
# Ops whose response this module describes. An explicit set, not duck typing:
# keying on `results` alone would also match memory.get_many (a dict),
# memory.related and knowledge.search, padding the abstain-rate denominator, and
# requiring `retrieval_risk` would drop memory.search_trace, which has no such
# field. `test_every_search_op_is_covered_by_the_shape_gate` fails if a new op
# grows a retrieval envelope without being added here.
# code.search deliberately mirrors memory.search's abstain wire fields so
# this same pipeline covers it (span attrs, Langfuse scores, abstain
# metrics). Its hits carry no cosine `distance`, so the quality histograms
# skip them harmlessly.
_SEARCH_OPS: frozenset[str] = frozenset(
    {"memory.search", "memory.search_trace", "memory.ask", "code.search", "knowledge.search"}
)

# Hit `source` values whose `distance` is a genuine cosine (knowledge hits
# mark themselves "knowledge"; their distance is the vector leg's cosine even
# in hybrid mode, where `score` is the fused rank).
_COSINE_PLANES: frozenset[str] = frozenset(
    {"fact", "chunk", "summary", "entity_summary", "observation", "anchor", "knowledge"}
)


def _clamp_ns(namespace: str | None) -> str:
    """Namespace is a bounded label in practice (one per tenant), but guard
    against an unbounded value arriving from a malformed request."""
    if not namespace:
        return "-"
    ns = str(namespace)
    return ns if len(ns) <= 64 else ns[:64]


def init_domain_metrics(meter: Any) -> None:
    """Create the domain instruments on an existing meter.

    Called from `otel.py` once the MeterProvider exists. Safe to call twice.
    """
    global _initialized
    if _initialized:
        return
    try:
        _init_search_instruments(meter)
        _init_memory_instruments(meter)
        _init_procedure_instruments(meter)
        _init_build_cost_instruments(meter)
        _init_cost_instruments(meter)
        _init_worker_instruments(meter)
        _init_auth_instruments(meter)
        from server.observability import pg_pipeline

        pg_pipeline.register(meter)
        _initialized = True
        log.info("loom domain metrics initialized")
    except Exception as e:  # noqa: BLE001 — metrics init must not stop startup
        log.warning("domain metrics init failed: %s", e)


def _init_search_instruments(meter: Any) -> None:
    """The retrieval-quality instruments."""
    global _search_results, _search_top_similarity, _search_confidence
    global _search_judged_relevance, _search_judge_verdict, _search_abstain
    global _search_plane_hits, _search_assist
    # --- Retrieval quality -------------------------------------------
    # The core question: did the search find anything, and did it know
    # how sure it was?
    _search_results = meter.create_histogram(
        name="loom.search.results",
        description="Number of results returned per search",
        unit="1",
    )
    # The single most diagnostic retrieval signal. A drifting top
    # similarity means the corpus, the embedder or the query mix changed
    # — visible here long before users report bad answers.
    _search_top_similarity = meter.create_histogram(
        name="loom.search.top_similarity",
        description="Cosine similarity of the best hit (1.0 - distance)",
        unit="1",
    )
    _search_confidence = meter.create_histogram(
        name="loom.search.confidence",
        description="Loom's own retrieval_confidence for the response",
        unit="1",
    )
    # The INDEPENDENT quality axis. `confidence` and `top_similarity` above
    # are loom grading itself against its own embedder's distance
    # distribution; this comes from a judge that shares none of that state, so
    # the two can disagree — and the disagreement is the signal.
    #
    # Emitted as a metric, not only as a Langfuse score, for two reasons: the
    # alert that pages on quality collapse should not depend on a third party
    # being up, and it belongs beside the self-graded series it is meant to be
    # compared against.
    _search_judged_relevance = meter.create_histogram(
        name="loom.search.judged_relevance",
        description="Independent LLM judge's relevance rating for a sampled search",
        unit="1",
    )
    _search_judge_verdict = meter.create_counter(
        name="loom.search.judge_verdict",
        description="Judge verdicts on sampled searches, by category",
        unit="1",
    )
    # Abstention is a feature, not an error — but a rising abstain rate is
    # the earliest signal that retrieval has stopped working.
    _search_abstain = meter.create_counter(
        name="loom.search.abstain",
        description="Searches that abstained, by reason",
        unit="1",
    )
    # Which retrieval plane actually produced the winning hits. Tells you
    # which legs earn their latency: if the chunk plane never wins, it is
    # costing you a round-trip for nothing.
    _search_plane_hits = meter.create_counter(
        name="loom.search.plane_hits",
        description="Hits contributed per retrieval plane/source",
        unit="1",
    )
    # Optional LLM assists (rerank / HyDE / iterative judge) each add 1-3s.
    # Counting them separates "search is slow" from "search needed help".
    _search_assist = meter.create_counter(
        name="loom.search.assist",
        description="Optional retrieval assists engaged, by kind",
        unit="1",
    )


def _init_memory_instruments(meter: Any) -> None:
    """The memory lifecycle instruments."""
    global _paid_errors, _memory_writes, _policy_decisions, _memory_drops
    global _enumeration_shaped, _attribute_tagged
    # --- Memory lifecycle --------------------------------------------
    # Belief revision outcomes. `outcome` is a closed enum, so this stays
    # low-cardinality while answering "are writes being merged, flagged as
    # conflicts, or rejected outright?"
    # A request that ran the model (or the embedder) and then
    # failed is money spent for nothing; the ledger row has the cost and
    # the error code, this counter puts them on the same panel as latency.
    _paid_errors = meter.create_counter(
        name="loom.ops.paid_errors",
        description="Failed requests that had already incurred upstream cost",
        unit="1",
    )
    _memory_writes = meter.create_counter(
        name="loom.memory.writes",
        description=(
            "Memory write outcomes (created/updated/merged/duplicate/"
            "conflicted/rejected/blacklisted/too_large/precondition_failed/"
            "error/unchanged/unknown)"
        ),
        unit="1",
    )

    # Ingest drop ledger (memory/app/drops.py). `stage` and `reason` are
    # bounded by convention (they also land in LowCardinality columns);
    # the free-text `item` never becomes a label.
    # History-policy gate verdicts (identity/app/policy_gate.py): mode × decision
    # × errored. In shadow mode `deny` counts what enforce WOULD block.
    _policy_decisions = meter.create_counter(
        name="loom.policy.decisions",
        description="History-policy gate decisions by mode, decision and errored",
        unit="1",
    )

    _memory_drops = meter.create_counter(
        name="loom.memory.drops",
        description=(
            "Items the memory ingest pipeline dropped, truncated or gave "
            "up on, by stage and reason (mirrors loom.memory_drops)"
        ),
        unit="1",
    )

    _enumeration_shaped = meter.create_counter(
        name="loom.memory.search.enumeration_shaped",
        description=(
            "memory.search queries shaped like a memory-extraction locator "
            "(asks for the stored records as a class); "
            "a signal, not a refusal"
        ),
        unit="1",
    )

    _attribute_tagged = meter.create_counter(
        name="loom.memory.attribute_tagged",
        description=(
            "Memory rows labelled attribute:<class> at write time — the value "
            "mentions a protected attribute or a proxy"
        ),
        unit="1",
    )


def _init_procedure_instruments(meter: Any) -> None:
    """The procedure plane instruments."""
    global _procedure_runs, _procedure_evolve
    # --- Procedure plane ---------------------------------------------
    # Runs an agent reported against a procedure, by outcome, and what
    # evolve did with them. Together they answer "are procedures being
    # exercised, do they work, and is consolidation converging?".
    _procedure_runs = meter.create_counter(
        name="loom.procedure.runs",
        description="Procedure executions reported via procedure.record_run, by outcome",
        unit="1",
    )
    _procedure_evolve = meter.create_counter(
        name="loom.procedure.evolve",
        description="procedure.evolve calls by result (changed/stable/template)",
        unit="1",
    )


def _init_build_cost_instruments(meter: Any) -> None:
    """The build-side cost instruments."""
    global _memory_build_tokens, _memory_extraction_yield, _extract_cost_per_row
    # --- Build-side cost --------------------------------
    # What it costs to BUILD memory, separately from what it costs to
    # read it. `loom.memory.build.tokens{stage,direction}` is the raw
    # spend; `loom.memory.extraction_yield` is prompt tokens per memory
    # actually written by one extraction call.
    _memory_build_tokens = meter.create_counter(
        name="loom.memory.build.tokens",
        description="LLM tokens spent building memory, by stage and direction (in/out)",
        unit="1",
    )
    _memory_extraction_yield, _extract_cost_per_row = _extraction_histograms(meter)


def _init_cost_instruments(meter: Any) -> None:
    """The spend, token, tracker and LLM instruments."""
    global _op_cost, _op_tokens, _tracker_sections, _tracker_section_ms, _llm_fallback
    global _llm_response_cache
    # --- Cost / tokens ------------------------------------------------
    # Authoritative spend stays in loom.agent_events; these put it in the
    # same store as latency and retrieval quality so a spend spike and a
    # latency spike can be correlated without a cross-store join.
    _op_cost = meter.create_counter(
        name="loom.cost.usd",
        description="Upstream provider spend attributed to an operation",
        unit="USD",
    )
    _op_tokens = meter.create_counter(
        name="loom.tokens",
        description="Tokens consumed, by kind (embed/llm_input/llm_output)",
        unit="1",
    )
    _tracker_sections, _tracker_section_ms = _tracker_instruments(meter)
    # A primary LLM provider gave up after its retries and another model
    # answered (server/llm/fallback.py). Attributes: from/to model, reason.
    _llm_fallback = meter.create_counter(
        name="loom.llm.fallback",
        description="LLM calls answered by the fallback model after a provider outage",
        unit="1",
    )
    # The exact-prompt response cache (server/llm/cache.py):
    # one increment per wrapped call, outcome hit | miss, by model.
    _llm_response_cache = meter.create_counter(
        name="loom.llm.response_cache",
        description="LLM calls checked against the exact-prompt response cache, by outcome",
        unit="1",
    )


def _init_worker_instruments(meter: Any) -> None:
    """The background worker instruments."""
    global _worker_passes, _worker_pass_duration
    # --- Background workers -------------------------------------------
    # An in-process snapshot alone would make a worker that fails every
    # other pass look healthy whenever you happen to look, with no pass
    # duration anywhere. `worker` is bounded by the
    # registry (~25 names); `outcome` is success|failure.
    _worker_passes = meter.create_counter(
        name="loom.worker.passes",
        description="Background worker passes, by worker and outcome (success|failure)",
        unit="1",
    )
    _worker_pass_duration = meter.create_histogram(
        name="loom.worker.pass_duration_ms",
        description="Wall-clock duration of one background worker pass",
        unit="ms",
    )


def _init_auth_instruments(meter: Any) -> None:
    """The authentication instruments."""
    global _auth_refusals, _agents_provisioned
    # A refused bearer has no op ledger row (the request has no principal yet;
    # a /v1/me/* route writes only its own me.* row) and never reaches the SLI
    # rollup, so this counter is the trace of a 401 storm or of every caller
    # getting 503 while the revocation list is unloaded.
    _auth_refusals = meter.create_counter(
        name="loom.auth.refusals",
        description=(
            "Requests refused for their bearer token (none, one that failed"
            " verification, or one that could not be checked yet), by reason and transport"
            " (http, mcp, or scim for the SCIM provider's token)"
        ),
        unit="1",
    )
    # A leaked issuer key or a runaway control plane names new agents without
    # end: each one costs a namespace, and the trust stops at --max-agents.
    # Trusts are registered by an operator, so `loom.trust` stays bounded.
    _agents_provisioned = meter.create_counter(
        name="loom.auth.agents_provisioned",
        description=(
            "First exchanges of an agent of a workload trust, by loom.org, loom.trust and"
            " outcome: made (its namespace was made) or at_cap (refused, the trust had"
            " made --max-agents)"
        ),
        unit="1",
    )


AUTH_REFUSAL_REASONS = ("missing", "malformed", "expired", "revoked", "invalid", "unavailable")


def auth_refusal_reason(error: BaseException | None) -> str:
    """The bounded reason for a refused bearer: `missing` when there was
    none, else the `reason` its TokenError was raised with."""
    if error is None:
        return "missing"
    reason = getattr(error, "reason", "invalid")
    return reason if reason in AUTH_REFUSAL_REASONS else "invalid"


def record_auth_refusal(error: BaseException | None, *, transport: str) -> None:
    """One tick of `loom.auth.refusals`. Never raises."""
    if _auth_refusals is None:
        return
    try:
        _auth_refusals.add(
            1,
            attributes={
                "reason": auth_refusal_reason(error),
                "loom.transport": transport,
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_agent_provisioned(org: str, trust: str, *, outcome: str) -> None:
    """One tick of `loom.auth.agents_provisioned`; `outcome` is made or at_cap.
    Never raises."""
    if _agents_provisioned is None:
        return
    try:
        _agents_provisioned.add(
            1, attributes={"loom.org": org, "loom.trust": trust, "outcome": outcome}
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_search_quality(
    op_name: str,
    *,
    namespace: str,
    result: Any,
    transport: str = "http",
    query: str = "",
    org: str = "",
) -> None:
    """Emit retrieval-quality metrics from a search response.

    Duck-typed on the response rather than keyed to an op name, so
    `memory.search`, `memory.search_trace` and any future search-shaped op are
    all covered without a branch per op. Never raises: a telemetry bug must
    not fail a served request.

    Deliberately NOT gated on the domain instruments existing. Those are created
    only under `exporter_mode == "ch"`, but span export is configured
    independently — returning early when they are None would leave OTLP
    deployments with no `loom.search.*` span attributes at all, and the
    documented "show me the searches that abstained" query would return zero
    rows. Span attribution runs first; the metric
    recording below is what checks for instruments.
    """
    try:
        get = result.get if isinstance(result, dict) else lambda k, d=None: getattr(result, k, d)
        hits = get("results", None)
        if op_name not in _SEARCH_OPS or not isinstance(hits, list):
            return  # not a search op, or not a list of hits

        attrs = {
            "loom.operation": op_name,
            "loom.namespace": _clamp_ns(namespace),
            "loom.transport": transport if transport in ("http", "mcp") else "other",
        }

        # ---- Phase 1: parse. No emission, so a shape surprise cannot cost us
        # the span attribution below.
        conf = get("retrieval_confidence", None)
        conf_f = float(conf) if isinstance(conf, (int, float)) else None

        # Best distance among hits whose `distance` is ACTUALLY a cosine.
        # `distance` is overloaded: record-plane rows store
        # max(0, 1 - lexical_rank_score) — token overlap — and injected
        # computed/derived evidence rows hardcode distance=0.0, i.e. a perfect
        # 1.0. A plain min over every hit would report near-perfect quality for
        # searches that abstained, and every aggregational query would record
        # exactly 1.0, pinning the decile buckets in the top bin so the panel
        # that exists to catch degradation would read healthy.
        best_distance = None
        per_plane: dict[str, int] = {}
        for h in hits:
            get_h = h.get if isinstance(h, dict) else lambda k, d=None, _h=h: getattr(_h, k, d)
            src = str(get_h("source", "") or "") or "unknown"
            per_plane[src[:32]] = per_plane.get(src[:32], 0) + 1
            if src not in _COSINE_PLANES:
                continue
            d = get_h("distance")
            if isinstance(d, (int, float)) and (best_distance is None or d < best_distance):
                best_distance = float(d)
        top_sim = (1.0 - best_distance) if best_distance is not None else None
        if top_sim is None:
            # code.search: its units carry a fused RRF `score`, not a cosine
            # distance, so the per-hit walk above finds nothing. The handler
            # exposes the top vector-leg cosine at response level instead;
            # without this fallback the retrieval-quality panel and its
            # alert would be blind to the whole code plane.
            rt = get("top_similarity")
            if isinstance(rt, (int, float)) and not isinstance(rt, bool) and rt > 0:
                top_sim = float(rt)

        abstained = bool(get("abstain", False))
        reason = str(get("abstain_reason", "") or "unspecified")[:48]
        code = ""
        if abstained:
            # The metric label MUST be the bounded code, never the prose reason:
            # `abstain_reason` interpolates the similarity and threshold values,
            # so every distinct float would mint a new series — truncating the
            # string bounds its length, not its cardinality. Classify by prefix
            # for a response without `abstain_code`.
            code = str(get("abstain_code", "") or "")
            if code not in (
                "no_match",
                "below_threshold",
                "low_calibrated_confidence",
                "entity_absent",
                "not_covered",
                "page_expired",
            ):
                if reason.startswith("no memories matched"):
                    code = "no_match"
                elif reason.startswith("top hit similarity"):
                    code = "below_threshold"
                elif reason.startswith("calibrated retrieval confidence"):
                    code = "low_calibrated_confidence"
                else:
                    code = "unspecified"

        # Assists are recorded into SearchContext.actual_work and reach the
        # response inside `retrieval_work`.
        work = get("retrieval_work", None)
        if not isinstance(work, dict):
            work = {}

        def _flag(*keys: str) -> bool:
            """True if any key is a truthy bool or a non-zero call counter."""
            for k in keys:
                v = work.get(k)
                if isinstance(v, bool) and v:
                    return True
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    return True
            return False

        assists = {
            "hyde": _flag("hyde_initial_used", "llm_hyde_calls"),
            "rerank": _flag("inline_rerank_used", "external_rerank_used"),
            "iterative_judge": _flag("llm_iterative_judge_calls"),
            # code.ask / code.search expand=true: LLM identifier proposal
            "query_expand": _flag("llm_query_expand_calls"),
        }
        used = [kind for kind, on in assists.items() if on]

        # ---- Phase 2: span attribution, FIRST, as the docstring promises.
        # Deliberately not gated on the domain instruments: those exist only
        # under exporter_mode == "ch", while span export is configured
        # independently, and the span is the only per-request record the
        # documented drill-downs can use.
        from server.observability import otel as _otel

        _otel.set_search_attributes(
            hits=len(hits),
            confidence=conf_f,
            top_similarity=top_sim,
            abstain=abstained,
            abstain_reason=reason if abstained else "",
            assists=used,
        )

        # Langfuse scores, from the same parsed values. Fire-and-forget: enqueued
        # onto a bounded queue and posted by a background task, so a slow or down
        # Langfuse cannot add latency to a search. Placed with span attribution
        # rather than with the metrics below because, like the span, it is
        # per-request and does not depend on the domain instruments existing.
        try:
            from server.observability import langfuse_scores as _scores

            _scores.record_search_scores(
                confidence=conf_f,
                top_similarity=top_sim,
                abstained=abstained,
                abstain_code=code,
                namespace=_clamp_ns(namespace),
            )
        except Exception:  # noqa: BLE001 — scores are additive; never fail a search
            pass

        # Independent judging of a sampled fraction. Here, not at the transports,
        # because this function is already the single funnel both of them call and
        # it has the parsed hits in hand — a second call site would drift.
        #
        # Off unless `retrieval_judge_sample_rate` > 0. Queued, never awaited: it
        # is an LLM call and this runs inside the request.
        try:
            if query:
                from server.observability import langfuse_scores as _sc
                from server.observability import retrieval_judge as _judge

                tid = _sc._current_trace_id()
                if tid:
                    _judge.submit(
                        trace_id=tid,
                        query=query,
                        hits=hits,
                        org=org,
                        namespace=_clamp_ns(namespace),
                    )
        except Exception:  # noqa: BLE001 — judging is additive
            pass

        # ---- Phase 3: metrics. Each instrument is guarded individually.
        if _search_results is not None:
            _search_results.record(len(hits), attributes=attrs)
        if conf_f is not None and _search_confidence is not None:
            _search_confidence.record(conf_f, attributes=attrs)
        if top_sim is not None and _search_top_similarity is not None:
            _search_top_similarity.record(top_sim, attributes=attrs)
        if abstained and _search_abstain is not None:
            _search_abstain.add(1, attributes={**attrs, "reason": code})
        if _search_plane_hits is not None:
            for plane, n in per_plane.items():
                _search_plane_hits.add(n, attributes={**attrs, "plane": plane})
        if _search_assist is not None:
            for kind in used:
                _search_assist.add(1, attributes={**attrs, "kind": kind})

    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_judged_relevance(
    *, relevance: float, verdict: str, namespace: str, scheme: str = ""
) -> None:
    """Record the independent judge's verdict for one sampled search.

    Called from the judge's background worker, not the request path. The verdict
    label is bounded to the judge's four categories — it is derived from model
    output, so an unbounded label here would let an LLM mint metric series.

    `scheme` names how `relevance` was DERIVED, so points from different
    scoring schemes are never averaged together; a point with no `scheme`
    attribute (`Attributes['scheme'] = ''`) has none.

    A bounded constant defined in code, not model output — so it does not
    reopen the cardinality hole the verdict label is careful about.
    """
    try:
        # Namespace only, matching every other domain metric. `loom.org` would be
        # a new label vector nobody else carries, and namespace is already the more
        # specific of the two — the cardinality discipline in this module is worth
        # more than the convenience.
        attrs = {"loom.namespace": _clamp_ns(namespace)}
        if scheme:
            attrs["scheme"] = scheme[:40]
        if _search_judged_relevance is not None:
            _search_judged_relevance.record(max(0.0, min(1.0, float(relevance))), attributes=attrs)
        if _search_judge_verdict is not None:
            bounded = (
                verdict
                if verdict in ("answers", "partial", "off_topic", "empty")
                else "unspecified"
            )
            _search_judge_verdict.add(1, attributes={**attrs, "verdict": bounded})
    except Exception:  # noqa: BLE001 — telemetry must never break the judge
        pass


def record_cost(
    op_name: str,
    *,
    namespace: str,
    cost_usd: float,
    embed_tokens: int,
    llm_input_tokens: int,
    llm_output_tokens: int,
) -> None:
    """Emit spend and token counters for one operation.

    Cost already lands in `loom.agent_events.upstream_cost_usd`, which is the
    billing-grade record and stays authoritative. This exists so cost sits in the
    SAME store as latency and retrieval quality: answering "the p99 doubled and
    spend tripled — same cause?" should not require joining a CH table against a
    metrics backend. Counters here are for trend and alerting, not invoicing.

    Called from `observability/ledger.emit_agent_event`, which both the HTTP and MCP
    transports funnel through, so one hook covers every op on both interfaces.
    """
    if _op_cost is None:
        return
    try:
        attrs = {"loom.operation": op_name, "loom.namespace": _clamp_ns(namespace)}
        # Skip zero-cost ops entirely: most operations touch no upstream, and
        # emitting a 0 for each would triple the series count for no signal.
        if cost_usd > 0:
            _op_cost.add(float(cost_usd), attributes=attrs)
        if embed_tokens:
            _op_tokens.add(int(embed_tokens), attributes={**attrs, "kind": "embed"})
        if llm_input_tokens:
            _op_tokens.add(int(llm_input_tokens), attributes={**attrs, "kind": "llm_input"})
        if llm_output_tokens:
            _op_tokens.add(int(llm_output_tokens), attributes={**attrs, "kind": "llm_output"})
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def _tracker_instruments(meter: Any) -> tuple[Any, Any]:
    """One tracker-source read per planned section of tracker.answer: served | empty |
    tracker_unavailable | restricted_required, by template — the rate of
    `tracker_unavailable` per template is what says a budget is wrong; `empty` per template
    says a statement is wrong — and the read's wall time by template."""
    sections = meter.create_counter(
        name="loom.tracker.sections",
        description="tracker.answer sections by template and status",
        unit="1",
    )
    section_ms = meter.create_histogram(
        name="loom.tracker.section_ms",
        description="Wall time of one tracker.answer section's read, by template",
        unit="ms",
    )
    return sections, section_ms


def record_tracker_section(*, template: str, status: str, ms: float, restricted: bool) -> None:
    """One tracker.answer section: its template, its status and the time its read
    took. Labels are bounded: templates come from the
    catalogue, statuses from a fixed set; no namespace, no question text."""
    if _tracker_sections is None:
        return
    try:
        attrs = {
            "loom.tracker.template": template,
            "loom.tracker.status": status,
            "loom.tracker.restricted": "1" if restricted else "0",
        }
        _tracker_sections.add(1, attributes=attrs)
        if _tracker_section_ms is not None and status not in ("restricted_required", "planned"):
            _tracker_section_ms.record(float(ms), attributes={"loom.tracker.template": template})
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


UNLISTED_MODEL = "unlisted"


def model_label(model: str) -> str:
    """A model name as a metric label: a model the pricing table lists or the
    deployment's own LLM / fallback model keeps its name, anything else is
    `unlisted`. A model name can arrive from a request (`memory.ask`'s
    `mid_model`) or a namespace's rerank endpoint, and as a raw label every
    distinct string would mint series."""
    from server.observability.cost_accumulator import PRICE_PER_1K_TOKENS

    name = str(model or "")
    if name in PRICE_PER_1K_TOKENS or name in (
        _settings.llm_model,
        _settings.llm_fallback_model.strip(),
    ):
        return name
    return UNLISTED_MODEL


def record_llm_fallback(*, from_model: str, to_model: str, reason: str) -> None:
    """One LLM call answered by the fallback model. Bounded labels: models
    through `model_label`, `reason` from server/llm/fallback.py (an HTTP status
    and the provider's error code, a timeout, a transport error)."""
    if _llm_fallback is None:
        return
    try:
        _llm_fallback.add(
            1,
            attributes={
                "loom.llm.from_model": model_label(from_model),
                "loom.llm.to_model": model_label(to_model),
                "loom.llm.fallback_reason": reason,
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def _extraction_histograms(meter: Any) -> tuple[Any, Any]:
    """The two build-side histograms: `loom.memory.extraction_yield` (prompt
    tokens per memory written) and the marginal cost of construction —
    provider dollars per row one set_from_messages wrote, by the namespace's
    kind (traffic class), so a change that doubles rows or halves calls moves
    a number an operator watches."""
    extraction_yield = meter.create_histogram(
        name="loom.memory.extraction_yield",
        description="Prompt tokens per memory written by one extraction call",
        unit="1",
    )
    cost_per_row = meter.create_histogram(
        name="loom.extract.cost_per_row_usd",
        description="Upstream USD per memory row written by one extraction, by namespace kind",
        unit="USD",
    )
    return extraction_yield, cost_per_row


def record_extract_cost_per_row(*, namespace_kind: str, cost_usd: float, rows: int) -> None:
    """One extraction's provider dollars per row written. A
    call that wrote nothing records its whole cost against one row, as the
    yield histogram does, so a namespace whose extraction writes nothing
    reads as the most expensive, not as free. Bounded label: the class."""
    if _extract_cost_per_row is None:
        return
    try:
        _extract_cost_per_row.record(
            float(cost_usd) / float(max(1, int(rows))),
            attributes={"loom.namespace_kind": str(namespace_kind or "unclassified")[:32]},
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_llm_response_cache(*, model: str, outcome: str) -> None:
    """One LLM call checked against the exact-prompt response cache. Bounded
    labels: the model through `model_label`, `outcome` is hit | miss."""
    if _llm_response_cache is None:
        return
    try:
        _llm_response_cache.add(
            1, attributes={"loom.llm.model": model_label(model), "loom.llm.cache_outcome": outcome}
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_worker_pass(name: str, *, success: bool, duration_ms: float) -> None:
    """One tick of `loom.worker.passes` + `loom.worker.pass_duration_ms`.

    Called from `server.workers.status.record_pass`. No-op until the meter
    exists (OTel off, CLI process without metrics). Never raises.
    """
    try:
        attrs = {"worker": _clamp_ns(name)}
        if _worker_passes is not None:
            _worker_passes.add(
                1, attributes={**attrs, "outcome": "success" if success else "failure"}
            )
        if _worker_pass_duration is not None:
            _worker_pass_duration.record(float(duration_ms), attributes=attrs)
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_memory_drop(*, namespace: str, stage: str, reason: str, n: int = 1) -> None:
    """Count `n` dropped items at `stage` for `reason` (both bounded enums)."""
    if _memory_drops is None:
        return
    try:
        _memory_drops.add(
            int(n),
            attributes={
                "loom.namespace": _clamp_ns(namespace),
                "stage": str(stage)[:32],
                "reason": str(reason)[:32],
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_attribute_tagged(*, namespace: str, attribute: str) -> None:
    """Count one attribute:<class> label written."""
    if _attribute_tagged is None:
        return
    try:
        _attribute_tagged.add(
            1, attributes={"loom.namespace": _clamp_ns(namespace), "attribute": str(attribute)[:32]}
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_enumeration_shaped(*, namespace: str) -> None:
    """Count one enumeration-shaped memory.search query."""
    if _enumeration_shaped is None:
        return
    try:
        _enumeration_shaped.add(1, attributes={"loom.namespace": _clamp_ns(namespace)})
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_policy_decision(*, namespace: str, mode: str, decision: str, errored: bool) -> None:
    """Count one history-policy gate decision (bounded labels)."""
    if _policy_decisions is None:
        return
    try:
        _policy_decisions.add(
            1,
            attributes={
                "loom.namespace": _clamp_ns(namespace),
                "mode": str(mode)[:16],
                "decision": str(decision)[:16],
                "errored": bool(errored),
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_procedure_run(*, namespace: str, ok: bool) -> None:
    """Count one reported procedure execution by outcome."""
    if _procedure_runs is None:
        return
    try:
        _procedure_runs.add(
            1,
            attributes={
                "loom.namespace": _clamp_ns(namespace),
                "outcome": "ok" if ok else "fail",
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_procedure_evolve(*, namespace: str, result: str) -> None:
    """Count one evolve call by result (changed / stable / template)."""
    if _procedure_evolve is None:
        return
    try:
        _procedure_evolve.add(
            1,
            attributes={
                "loom.namespace": _clamp_ns(namespace),
                "result": str(result)[:16],
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


# `messages` on loom.memory.extraction_yield is the caller's batch size. As a
# raw number it would multiply namespace x stage x histogram buckets by up to
# 1000 values; these ranges hold the label to six.
MESSAGES_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (1, "1"),
    (5, "2-5"),
    (20, "6-20"),
    (100, "21-100"),
)
MESSAGES_BUCKET_OVER = "101+"


def messages_bucket(messages: int) -> str:
    """The `messages` label for a batch of this many messages:
    0 | 1 | 2-5 | 6-20 | 21-100 | 101+."""
    n = max(0, int(messages))
    return next((label for top, label in MESSAGES_BUCKETS if n <= top), MESSAGES_BUCKET_OVER)


def record_extraction_build(
    *,
    namespace: str,
    tokens_in: int,
    tokens_out: int,
    written: int,
    messages: int = 0,
    stage: str = "extract",
) -> None:
    """Account one extraction call on the build side: tokens by direction and
    the yield (prompt tokens per memory written). A call that wrote nothing
    records its tokens against a yield of the full prompt (written treated as
    1), so a namespace whose extraction produces no memories reads as the
    most expensive, not as free. Never raises."""
    if _memory_build_tokens is None and _memory_extraction_yield is None:
        return
    try:
        attrs = {"loom.namespace": _clamp_ns(namespace), "stage": str(stage)[:32]}
        if _memory_build_tokens is not None:
            if tokens_in:
                _memory_build_tokens.add(int(tokens_in), attributes={**attrs, "direction": "in"})
            if tokens_out:
                _memory_build_tokens.add(int(tokens_out), attributes={**attrs, "direction": "out"})
        if _memory_extraction_yield is not None and tokens_in:
            _memory_extraction_yield.record(
                float(tokens_in) / float(max(1, written)),
                attributes={**attrs, "messages": messages_bucket(messages)},
            )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_paid_error(op_name: str, *, namespace: str, error_code: str) -> None:
    """Count a failed request that had already paid an upstream call."""
    if _paid_errors is None:
        return
    try:
        _paid_errors.add(
            1,
            attributes={
                "loom.operation": str(op_name)[:64],
                "loom.namespace": _clamp_ns(namespace),
                "error_code": str(error_code or "")[:32],
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def record_memory_write(*, namespace: str, outcome: str) -> None:
    """Count a belief-revision outcome. `outcome` must be a bounded enum."""
    if _memory_writes is None:
        return
    try:
        _memory_writes.add(
            1,
            attributes={
                "loom.namespace": _clamp_ns(namespace),
                "outcome": str(outcome)[:24],
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass
