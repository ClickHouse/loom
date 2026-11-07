"""The code.search handler: the single-namespace search, its memo, cursor pages and
the post-ranking steps (risk annotations, top-hit escalation)."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app.common import (
    answer_args,
    embedding_coverage_note,
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    resolve_code_thresholds,
    schedule_code_trace,
    unit_provenance,
)
from server.code.app.search.shared import (
    _not_covered_output,
    _partial_fields,
    _read_pool_settling,
    _scope_of,
    page_plan,
)
from server.code.app.search.state import CodeSearchResult
from server.code.app.search_legs import MODEL_LEG_MIN_MS, resolve_deadline_ms
from server.code.app.spans import (
    code_span,
    current_stage_ms,
    set_code_span_attributes,
    start_stage_wall,
    with_stage_ms,
)
from server.code.domain import deadline as code_deadline
from server.code.store.index import CodeRepo
from server.core.ch import caller_shaped
from server.core.llm_free import reads_llm_free
from server.identity.domain.read_scope import ReadFilters
from server.settings import settings

logger = logging.getLogger(__name__)


async def handle_code_search(input: CodeSearchInput) -> CodeSearchOutput:
    """code.search. A pattern or path globs put the caller's own expression into the
    scans, so such a search runs under the caller-shaped ClickHouse ceilings."""
    if not (input.pattern or input.path_globs or input.exclude_globs):
        return await _code_search(input)
    with caller_shaped():
        return await _code_search(input)


async def _code_search(input: CodeSearchInput) -> CodeSearchOutput:
    require_code_intel()
    # Several namespaces: each authorized, the single-namespace search run per namespace, the
    # ranks interleaved under one budget and one deadline. Unset: the single-namespace search.
    if input.namespaces:
        from server.code.app import search_fanout as fanout

        if len(input.namespaces) >= 2:
            return await fanout.fan_out(input, handle_code_search)
        input = await fanout.normalize(input)
    started = time.monotonic()
    start_stage_wall()  # every code_span below reports into it
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    budget = code_deadline.start(resolve_deadline_ms(input.deadline_ms), started=started)

    # A path outside the indexed prefixes is "not covered", not "no match":
    # the agent should go read the file, not conclude the code doesn't exist.
    not_covered = _not_covered_output(org, ns, input.path_prefix, CodeSearchOutput)
    if not_covered is not None:
        not_covered.as_of = await index_as_of(repo, org, ns)
        set_code_span_attributes(
            route="not_covered",
            results=0,
            abstain=True,
            abstain_code="not_covered",
            retrieval_risk="abstain",
            not_covered=True,
            consumer=input.consumer,
        )
        return not_covered

    # A continuation is a point read of the pool an earlier call ranked: no thresholds, no
    # embedder, no legs. When the pool row is gone (not landed, expired) the page is
    # re-searched and says so.
    if input.cursor:
        page, fallback, offset = await _serve_search_page(input, org, ns, repo, started)
        if page is not None:
            return page
        return await _page_by_research(input, offset=offset, reason=fallback)

    abstain_threshold, noise_floor = await resolve_code_thresholds(org, ns)

    embedder, embedding_model = _search_embedder(input)

    recent_dirs: list[str] = []
    # The transport's read-scope gate pins user_id to a narrowed token's user.
    if input.user_id and input.user_id != "-":
        recent_dirs = await _user_recent_dirs(org, input.user_id)

    extra_terms: list[str] | None = None
    expand_calls = 0
    if (
        input.expand
        and not reads_llm_free()  # an LLM-free read expands no query
        and _budget_allows(budget, "query expansion", MODEL_LEG_MIN_MS)
    ):
        from server.code.app.answers import _propose_identifiers
        from server.observability.budget import namespace_over_cap

        if not await namespace_over_cap(org, ns):
            from server.llm.client import get_llm_client

            with code_span("search.query_expand"):
                extra_terms = await _propose_identifiers(get_llm_client(), input.query)
            expand_calls = 1

    # Risk annotations are paid for INSIDE the request budget: reserve their
    # headroom before body-fill so the budget ceiling holds either way, then
    # add the measured spend back onto tokens_returned below.
    from server.code.app import risk as code_risk
    from server.code.app.search import search as run_search

    plan = page_plan(input)
    token_budget, bodies, annotate, reserve = (
        plan.token_budget,
        plan.bodies,
        plan.annotate,
        plan.reserve,
    )
    # The annotation's path reads start inside the search, on the ranked pool's head; the
    # served units are annotated from them below
    prefetch = code_risk.RiskPrefetch(repo, org, ns, top_k=input.top_k) if annotate else None

    # Constrained search: validated once here; unset fields give the empty scope and the
    # legs' statements are unchanged.
    scope = _scope_of(input)

    # A repeated search is a point read: the key covers every field that shapes the result; a
    # hit replays the stored response with the current freshness block and the memo's own
    # commit disclosed.
    from server.code.app import search_memo as memo

    memo_key = memo.key_for(
        input.model_copy(update={"bodies": bodies}),
        embedding_model=embedding_model,
        rerank=input.rerank,
        token_budget=token_budget,
        recent_dirs=recent_dirs,
    )
    # a seen-set shapes the result but is not in the memo key: skip the memo
    memo_on = memo.enabled(input) and not input.exclude_unit_ids
    if memo_on:
        with code_span("search.memo_probe"):
            hit = await memo.get(org, ns, memo_key)
        if hit is not None:
            return await _replay_memo_hit(
                hit,
                input,
                org,
                ns,
                repo,
                embedding_model=embedding_model,
                started=started,
            )

    res = await run_search(
        repo,
        org,
        ns,
        query=input.query,
        extra_terms=extra_terms,
        top_k=input.top_k,
        token_budget=max(200, token_budget - reserve),
        path_prefix=input.path_prefix,
        exclude_unit_ids=input.exclude_unit_ids or None,
        user_recent_dirs=recent_dirs or None,
        rerank_endpoint=input.rerank,
        embedder=embedder,
        abstain_threshold=abstain_threshold,
        kind_prior=input.kind_prior,
        pr_leg_before=input.pr_leg_before,
        embedding_model=input.embedding_model,
        noise_floor=noise_floor,
        bodies=bodies,
        min_fan_in=input.min_fan_in,
        sort_by=input.sort_by,
        scope=scope,
        legs=input.legs,
        track_path=input.track_path,
        vector_model=input.vector_model,
        vector_shortlist=input.vector_shortlist,
        risk_prefetch=prefetch,
        llm_legs=input.llm_legs,
        deadline_ms=input.deadline_ms,
        budget=budget,
    )

    if expand_calls:
        # counted as an assist ("query_expand") by the retrieval metrics
        res.retrieval_work["llm_query_expand_calls"] = expand_calls

    if (
        prefetch is not None
        and res.units
        and _budget_allows(budget, "risk annotation", _LATE_READ_MS)
    ):
        res.tokens_returned += await _annotate_served(res, prefetch, reserve=reserve)

    # Generous-budget escalation: when plenty of budget survives body-fill,
    # expand the TOP hit's body toward its whole indexed file (measured
    # optimum serving grows toward whole-file at large budgets). Auto mode
    # only; the ceiling still binds; disclosed via granularity="file".
    if bodies == "auto" and res.units and not res.abstain:
        await _escalate_top_to_file(repo, org, ns, res, token_budget, embedder, embedding_model)

    return await _finish_search(
        input,
        org,
        ns,
        repo,
        res,
        budget=budget,
        started=started,
        embedding_model=embedding_model,
        memo_key=memo_key if memo_on else None,
    )


def _search_embedder(input: CodeSearchInput) -> tuple[Any, str]:
    """The embedder the legs query with (None: lexical only) and the label they read: the
    embedder's, or the request's twin label — keyed into the memo, the trace and the
    cursor. A `vector_model` the deployment does not allow is refused."""
    embedder = None
    try:
        from server.embedding.embedder import get_embedder

        embedder = get_embedder()
    except Exception as e:  # noqa: BLE001 — lexical-only degrade
        logger.warning("code.search: embedder unavailable (%s)", e)

    embedding_model = input.embedding_model or (getattr(embedder, "model_name", "") or "")
    if input.vector_model:
        # A second vector model is a deployment allow-list entry, refused as bad input otherwise
        # (never a silent fallback).
        from server.core.errors import ValidationError
        from server.embedding.embedder import allowed_vector_models

        if input.vector_model not in allowed_vector_models():
            raise ValidationError(
                f"vector_model {input.vector_model!r} is not enabled on this deployment"
            )
    return embedder, embedding_model


async def _finish_search(
    input: CodeSearchInput,
    org: str,
    ns: str,
    repo: CodeRepo,
    res: CodeSearchResult,
    *,
    budget: Any,
    started: float,
    embedding_model: str,
    memo_key: str | None,
) -> CodeSearchOutput:
    """The trace row, the span, the cursor and the output of a searched answer; memoized
    under `memo_key` unless it is transient."""
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.search",
        query=input.query,
        route=res.route,
        returned_ids=[u.unit_id for u in res.units],
        returned_paths=[u.path for u in res.units],
        returned_scores=[u.score for u in res.units],
        top_k=input.top_k,
        tokens_returned=res.tokens_returned,
        abstain=res.abstain,
        retrieval_risk=res.retrieval_risk,
        embedding_model=embedding_model,
        args=_search_trace_args(input),
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        **unit_provenance(res.units),
    )
    set_code_span_attributes(
        route=res.route,
        tier=_units_tier(res.units),
        results=len(res.units),
        tokens_returned=res.tokens_returned,
        retrieval_risk=res.retrieval_risk,
        abstain=res.abstain,
        abstain_code=res.abstain_code,
        consumer=input.consumer,
        top_similarity=res.top_similarity,
        constrained=res.constrained or None,
        matched_files=res.matched_files or None,
    )

    as_of = await index_as_of(repo, org, ns)
    next_cursor = _issue_search_cursor(
        input,
        org,
        ns,
        res,
        run_id=trace_run_id,
        embedding_model=embedding_model,
        indexed_commit=str(as_of.get("commit") or ""),
    )
    out = CodeSearchOutput(
        results=res.units,
        next_cursor=next_cursor,
        has_more=bool(next_cursor),
        route=res.route,
        abstain=res.abstain,
        as_of=as_of,
        abstain_code=res.abstain_code,  # type: ignore[arg-type]
        abstain_reason=res.abstain_reason,
        retrieval_risk=res.retrieval_risk,  # type: ignore[arg-type]
        retrieval_confidence=res.retrieval_confidence,
        route_agreement=res.route_agreement,
        calibration_explanation=res.calibration_explanation,
        tokens_returned=res.tokens_returned,
        score_explanation=res.score_explanation,
        retrieval_work=with_stage_ms(res.retrieval_work),
        trace_run_id=trace_run_id,
        top_similarity=res.top_similarity,
        history=res.history,
        constrained=res.constrained,
        matched_files=res.matched_files,
        tracked=res.tracked,
        note=await embedding_coverage_note(repo, org, ns),
        legs_timed_out=res.legs_timed_out,
        epistemic=res.epistemic,  # type: ignore[arg-type]
        **_partial_fields(budget),
    )
    # a failed history read, a failed leg and a deadline-cut answer are transient: never replay
    if (
        memo_key is not None
        and not (res.history or {}).get("failed")
        and not out.partial
        and not res.epistemic
    ):
        from server.code.app import search_memo as memo

        memo.put(org, ns, memo_key, out, indexed_commit=str(as_of.get("commit") or ""))
    return out


async def _annotate_served(res: Any, prefetch: Any, *, reserve: int) -> int:
    """The risk block on the served units, from the prefetch that ran beside the post-fusion
    stages. Returns the tokens spent; never fails the search. What the pool head did not
    carry is disclosed only when it happened, so the payload is otherwise byte-identical to
    the sequential `code_risk.annotate_units`; with the precomputed table on, the source of
    the block and the count of units read live are disclosed on every annotated answer."""
    try:
        with code_span("search.risk_annotate", results=len(res.units)):
            spent = int(await prefetch.annotate(res.units, reserve_tokens=reserve))
        if prefetch.missed:
            res.retrieval_work["risk_annotate_missed"] = prefetch.missed
        # Where the block came from, and how many served units were read live (row missing or
        # older than code_unit_risk_max_age_s)
        if prefetch.source:
            res.retrieval_work["risk_source"] = prefetch.source
            res.retrieval_work["risk_stale_units"] = prefetch.stale
        return spent
    except Exception:
        logger.warning("code.search: risk annotation failed", exc_info=True)
        return 0


async def _replay_memo_hit(
    hit: dict[str, Any],
    input: CodeSearchInput,
    org: str,
    ns: str,
    repo: CodeRepo,
    *,
    embedding_model: str,
    started: float,
) -> CodeSearchOutput:
    """A memo hit replayed with the current freshness block, traced and stamped on the span like
    a computed search."""
    from server.code.app import search_memo as memo

    trace_run_id = new_trace_run_id()
    out: CodeSearchOutput = memo.replay(
        hit,
        CodeSearchOutput,
        as_of=await index_as_of(repo, org, ns),
        trace_run_id=trace_run_id,
    )
    # The coverage is this call's, not the stored search's
    out.note = await embedding_coverage_note(repo, org, ns)
    # this call's own wall (the memo probe), not the stored computation's
    with_stage_ms(out.retrieval_work)
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.search",
        query=input.query,
        route=out.route,
        args=_search_trace_args(input),
        returned_ids=[u.unit_id for u in out.results],
        returned_paths=[u.path for u in out.results],
        returned_scores=[u.score for u in out.results],
        top_k=input.top_k,
        tokens_returned=out.tokens_returned,
        abstain=out.abstain,
        retrieval_risk=out.retrieval_risk,
        embedding_model=embedding_model,
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        **unit_provenance(out.results),
    )
    set_code_span_attributes(
        route=out.route,
        results=len(out.results),
        tokens_returned=out.tokens_returned,
        retrieval_risk=out.retrieval_risk,
        abstain=out.abstain,
        consumer=input.consumer,
        memo_hit=True,
        memo_stale=bool(out.as_of.get("memo_stale")),
    )
    return out


def _units_tier(units: list[Any]) -> str:
    """Bounded tier of a unit list: the units' own tier field when they carry
    one (symbol-route units do), else '' (chunk units have no tier)."""
    from server.code.app.common import tier_of

    return tier_of([u for u in units if getattr(u, "tier", "")])


# ---------------------------------------------------------------------------
# Continuation: the next page is a point read of what was ranked. The pool row lives in
# loom.code_search_memo (the search memo's table) under cursor:s:<run id>; the cursor
# carries the offset, the hash of the ranking-shaping fields, the commit at issue and a
# checksum.
# ---------------------------------------------------------------------------


def _embedding_model_name() -> str:
    try:
        from server.embedding.embedder import get_embedder

        return getattr(get_embedder(), "model_name", "") or ""
    except Exception:  # noqa: BLE001 — lexical-only deployments have none
        return ""


def _search_params_hash(input: CodeSearchInput, embedding_model: str) -> str:
    from server.code.app import search_cursor as paging

    return paging.params_hash(input, rerank=input.rerank, embedding_model=embedding_model)


def _issue_search_cursor(
    input: CodeSearchInput,
    org: str,
    ns: str,
    res: CodeSearchResult,
    *,
    run_id: str,
    embedding_model: str,
    indexed_commit: str,
) -> str:
    """Store the ranked pool (fire-and-forget) and return the cursor for
    the page after the served one; "" when nothing remains, the search
    abstained (the agent was told to grep), the route ranked no pool
    (symbol routes) or the tier is off."""
    from server.code.app import search_cursor as paging

    pool = list(getattr(res, "pool", []) or [])
    served = len(res.units)
    if not paging.enabled() or res.abstain or len(pool) <= served:
        return ""
    ph = _search_params_hash(input, embedding_model)
    payload = paging.search_pool_payload(
        query=input.query,
        route=res.route,
        model=embedding_model,
        params=ph,
        served=served,
        pool=pool,
        calibration={
            "retrieval_risk": res.retrieval_risk,
            "retrieval_confidence": res.retrieval_confidence,
            "route_agreement": res.route_agreement,
            "calibration_explanation": res.calibration_explanation,
            "top_similarity": res.top_similarity,
            "score_explanation": res.score_explanation,
        },
    )
    paging.put_pool(
        org,
        ns,
        paging.pool_key("s", run_id),
        payload,
        indexed_commit=indexed_commit,
        route=res.route,
    )
    return paging.encode(
        kind="s", run_id=run_id, offset=served, params=ph, indexed_commit=indexed_commit
    )


async def _serve_search_page(
    input: CodeSearchInput, org: str, ns: str, repo: CodeRepo, started: float
) -> tuple[CodeSearchOutput | None, str, int]:
    """(page, "", offset) served from the pool row, or (None, reason, offset)
    when the row could not be read — the caller re-searches and discloses
    the reason. A cursor this server did not issue for this request shape
    is a 400."""
    from server.code.app import risk as code_risk
    from server.code.app import search_cursor as paging
    from server.code.domain.units import budget_units
    from server.core.errors import ValidationError

    model = input.embedding_model or _embedding_model_name()
    ph = _search_params_hash(input, model)
    try:
        fields = paging.decode(input.cursor, kind="s", params=ph)
    except paging.CursorError as e:
        raise ValidationError(f"invalid cursor: {e}") from e
    offset = int(fields["o"])
    if paging.expired(fields):
        return None, "expired", offset
    with code_span("search.page_read"):
        row = await _read_pool_settling(org, ns, paging.pool_key("s", str(fields["r"])), fields)
    if row is None:
        return None, "pool_not_found", offset
    payload = row["payload"]
    pool = list(payload.get("pool") or [])
    entries, next_offset, has_more = paging.take_page(
        pool, offset=offset, size=input.top_k, exclude=set(input.exclude_unit_ids)
    )
    rows: list[dict[str, Any]] = []
    if entries:
        with code_span("search.page_bodies", results=len(entries)):
            rows = await repo.chunks_by_keys(
                org,
                ns,
                str(payload.get("model") or model),
                [(str(e["path"]), str(e["id"])) for e in entries],
            )
    units = paging.rebuild_units(entries, rows)

    # the plan page 1 resolved
    plan = page_plan(input)
    units, tokens = budget_units(
        units, max(200, plan.token_budget - plan.reserve), bodies=plan.bodies
    )
    if plan.annotate and units:
        try:
            with code_span("search.risk_annotate", results=len(units)):
                tokens += await code_risk.annotate_units(
                    repo, org, ns, units, reserve_tokens=plan.reserve
                )
        except Exception:
            logger.warning("code.search page: risk annotation failed", exc_info=True)

    calib = dict(payload.get("calib") or {})
    route = f"{payload.get('route') or 'hybrid'}+page"
    next_cursor = ""
    if has_more:
        next_cursor = paging.encode(
            kind="s",
            run_id=str(fields["r"]),
            offset=next_offset,
            params=ph,
            indexed_commit=row["indexed_commit"],
            ts=int(fields["t"]),
        )
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.search",
        query=input.query,
        route=route,
        args=_search_trace_args(input),
        returned_ids=[u.unit_id for u in units],
        returned_paths=[u.path for u in units],
        returned_scores=[u.score for u in units],
        top_k=input.top_k,
        tokens_returned=tokens,
        abstain=False,
        retrieval_risk=str(calib.get("retrieval_risk") or "low"),
        embedding_model=str(payload.get("model") or model),
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route=route,
        results=len(units),
        tokens_returned=tokens,
        retrieval_risk=str(calib.get("retrieval_risk") or "low"),
        abstain=False,
        consumer=input.consumer,
        page_offset=offset,
    )
    return (
        CodeSearchOutput(
            results=units,
            next_cursor=next_cursor,
            has_more=has_more,
            route=route,
            as_of=paging.disclose(
                await index_as_of(repo, org, ns),
                page_commit=str(row["indexed_commit"] or ""),
                offset=offset,
                fallback="",
            ),
            retrieval_risk=calib.get("retrieval_risk") or "low",
            retrieval_confidence=float(calib.get("retrieval_confidence") or 0.0),
            route_agreement=float(calib.get("route_agreement") or 0.0),
            calibration_explanation=str(calib.get("calibration_explanation") or ""),
            tokens_returned=tokens,
            score_explanation=str(calib.get("score_explanation") or "") + ", page=pool",
            retrieval_work=with_stage_ms(
                {
                    "lexical_queries": 0,
                    "vector_queries": 0,
                    "symbol_queries": 0,
                    "embedding_calls": 0,
                    "page_read": 1,
                    "body_rows": len(rows),
                    "excluded_units": max(0, next_offset - offset - len(entries)),
                }
            ),
            trace_run_id=trace_run_id,
            top_similarity=float(calib.get("top_similarity") or 0.0),
        ),
        "",
        offset,
    )


async def _page_by_research(
    input: CodeSearchInput, *, offset: int, reason: str
) -> CodeSearchOutput:
    """The pool row is gone: compute the search again through the ordinary
    path, deep enough to reach the requested offset (budget scaled with
    the depth so the tail keeps its bodies), serve the slice past the
    offset, and disclose why. The recomputed call issues its own cursor
    for the page after this one."""
    from server.code.app import search_cursor as paging
    from server.code.app.search import _POOL_KEEP, classify_query_shape
    from server.code.domain.units import estimate_unit_tokens

    size = input.top_k
    want = max(size, min(_POOL_KEEP, offset + size))
    budget = input.token_budget or (1200 if classify_query_shape(input.query) == "symbol" else 3000)
    fresh = input.model_copy(
        update={
            "cursor": "",
            "top_k": want,
            "fresh": True,
            "token_budget": min(12000, int(budget * want / size)),
        }
    )
    out = await handle_code_search(fresh)
    out.results = out.results[offset : offset + size]
    out.tokens_returned = sum(estimate_unit_tokens(u) for u in out.results)
    out.route = f"{out.route}+page_fallback"
    out.as_of = paging.disclose(out.as_of, page_commit="", offset=offset, fallback=reason)
    set_code_span_attributes(page_offset=offset, page_fallback=reason)
    return out


# What a late optional read (the risk block, the whole-file escalation) needs left
# of a request budget to start.
_LATE_READ_MS = 100


def _budget_allows(budget: code_deadline.Budget | None, stage: str, need_ms: int) -> bool:
    """True without a budget or with `need_ms` left; otherwise `stage` is recorded as
    skipped."""
    if budget is None or budget.allows(need_ms):
        return True
    budget.drop(f"{stage} skipped")
    return False


async def _escalate_top_to_file(
    repo: CodeRepo,
    org: str,
    ns: str,
    res: CodeSearchResult,
    token_budget: int,
    embedder: Any,
    model: str,
) -> None:
    """The whole-file escalation of the top hit when at least 2000 tokens of the budget
    survive body-fill and the request's time budget has room; never fails the search."""
    leftover = token_budget - res.tokens_returned
    if leftover < 2000 or not _budget_allows(res.budget, "whole-file expansion", _LATE_READ_MS):
        return
    try:
        with code_span("search.expand_top_to_file"):
            res.tokens_returned += await _expand_top_to_file(
                repo, org, ns, res.units, leftover, embedder, model=model
            )
    except Exception:
        logger.debug("code.search: whole-file expansion skipped", exc_info=True)


async def _expand_top_to_file(
    repo: CodeRepo,
    org: str,
    ns: str,
    units: list[Any],
    leftover: int,
    embedder: Any,
    model: str = "",
) -> int:
    """Expand the top hit's body to its whole indexed file when it fits in
    the leftover budget. Returns tokens spent (0 = didn't fit / no gain).
    The 'file' is the file's indexed chunks in line order — chunk coverage,
    not byte-perfect source, same floor contract as everything else."""
    from server.code.domain.units import estimate_tokens

    top = units[0]
    if not top.path or top.granularity == "file":
        return 0
    model = model or getattr(embedder, "model_name", "") or settings.embedding_model
    rows = await repo.live_chunks_for_path(org, ns, top.path, model)
    if len(rows) < 2:
        return 0  # single-chunk file: the unit already IS the file
    rows.sort(key=lambda r: (int(r["start_line"] or 0), int(r["end_line"] or 0)))
    seen: set[tuple[int, int]] = set()
    parts: list[str] = []
    end_max = 0
    for r in rows:
        span = (int(r["start_line"] or 0), int(r["end_line"] or 0))
        if span in seen:
            continue
        seen.add(span)
        parts.append(str(r["content"]))
        end_max = max(end_max, span[1])
    text = "\n".join(parts)
    cost = estimate_tokens(text) - estimate_tokens(top.body)
    if cost <= 0 or cost > leftover * 0.9:
        return 0
    units[0] = top.model_copy(
        update={
            "body": text,
            "elided": False,
            "granularity": "file",
            "start_line": 1,
            "end_line": end_max or top.end_line,
        }
    )
    return cost


def _search_trace_args(input: CodeSearchInput) -> dict[str, Any]:
    """The search knobs the request set (None = server default, dropped)."""
    return answer_args(
        input,
        "top_k",
        "legs",
        "rerank",
        "bodies",
        "expand",
        "kind_prior",
        "path_globs",
        "exclude_globs",
        "langs",
        "exclude_unit_ids",
        "min_fan_in",
        "sort_by",
        "token_budget",
        "vector_model",
        "deadline_ms",
        fresh=input.fresh or None,
        paged=bool(input.cursor) or None,
    )


async def _user_recent_dirs(org: str, user_id: str) -> list[str]:
    """5-min-cached directory prefixes from the user's recent memories' files facets
    (personalization — tie-break input only), from the namespaces the caller may read
    and inside the request's read scope."""
    from server.auth.context import current_auth, resolve_my_namespaces
    from server.core.errors import AuthInsufficientError, IdentityIncompleteError
    from server.identity.domain.read_scope import request_read_filters

    try:
        auth = current_auth()
        # A scope that does not resolve personalizes nothing (fail closed).
        scope = request_read_filters(auth)
        namespaces = tuple(await resolve_my_namespaces(org)) if auth is not None else None
    except (AuthInsufficientError, IdentityIncompleteError):
        return []
    except Exception as e:  # noqa: BLE001 — personalization is best-effort
        logger.debug("code.search: caller namespaces unavailable: %s", e)
        return []
    key = (org, user_id, namespaces, scope)
    now = time.monotonic()
    cached = _RECENT_DIR_CACHE.get(key)
    if cached is not None and now - cached[0] < 300:
        return cached[1]
    dirs: list[str] = []
    try:
        from server.memory.app.code_plane_reads import user_file_dirs

        dirs = await user_file_dirs(org, user_id, namespaces, scope=scope)
    except Exception as e:  # noqa: BLE001 — personalization is best-effort
        logger.debug("code.search: recent-dirs lookup failed: %s", e)
    if len(_RECENT_DIR_CACHE) > 2000:
        _RECENT_DIR_CACHE.clear()
    _RECENT_DIR_CACHE[key] = (now, dirs[:50])
    return dirs[:50]


_RECENT_DIR_CACHE: dict[
    tuple[str, str, tuple[str, ...] | None, ReadFilters], tuple[float, list[str]]
] = {}
