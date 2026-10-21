"""Cost plane handlers (`cost.summary`, `cost.budget_status`) — read the
agent_cost_rollup_daily (refreshed once a minute) plus the live tail
of agent_events newer than its mark, so a readout is exact at any instant.

Wire models are imported lazily inside the handlers to keep the
operations→handlers layering acyclic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.ch import get_client
from server.core.naming import named
from server.insights.api.models_cost import (
    BudgetStatusInput,
    BudgetStatusOutput,
    CostAgentSummaryInput,
    CostAgentSummaryOutput,
    CostAgentSummaryRow,
    CostRecordTurnsInput,
    CostRecordTurnsOutput,
    CostSummaryInput,
    CostSummaryOutput,
    CostSummaryRow,
)
from server.insights.store import cost as cost_store
from server.observability.budget import effective_cap

log = logging.getLogger(__name__)

# The events-only dimensions (memory_key, user_id, model, traffic_class,
# instance_label, build_commit) and the cache columns exist only on the direct
# scan; a roll-up row reads them as None / 0.
_SCAN_DIMS = ("memory_key", "user_id", "model", "traffic_class", "instance_label", "build_commit")

# Ordinary clock skew; a turn dated further ahead counts in turns_dated_ahead.
_AHEAD_SLACK = timedelta(seconds=5)


def _summary_row(r: dict[str, Any], *, scan: bool) -> CostSummaryRow:
    lat_q = r["lat_q"]  # tuple/array of (p50, p95, p99)
    dims = {d: r.get(d) for d in _SCAN_DIMS} if scan else {}
    return CostSummaryRow(
        namespace=r.get("namespace"),
        agent=r.get("agent"),
        day=r.get("day"),
        operation=r.get("operation"),
        **dims,
        ops=int(r["ops"]),
        errors=int(r["errors"]),
        tokens=int(r["tokens"]),
        embed_tokens=int(r["embed_tokens"]),
        cost_usd=float(r["cost_usd"]),
        latency_p50_ms=float(lat_q[0]) if lat_q else 0.0,
        latency_p95_ms=float(lat_q[1]) if lat_q else 0.0,
        latency_p99_ms=float(lat_q[2]) if lat_q else 0.0,
        cache_hits=int(r.get("cache_hits") or 0) if scan else 0,
        cache_saved_usd=float(r.get("cache_saved_usd") or 0.0) if scan else 0.0,
    )


async def handle_cost_summary(input: CostSummaryInput) -> CostSummaryOutput:
    ch = get_client()

    dim_cols = list(input.group_by)
    # memory_key, user_id, model, traffic_class, instance_label and build_commit are
    # not carried by the MV. When the caller groups by (or filters on) any, fall back
    # to a direct scan of loom.agent_events (slower but accurate).
    needs_events_scan = (
        "memory_key" in dim_cols
        or "user_id" in dim_cols
        or "model" in dim_cols
        or "traffic_class" in dim_cols
        or "instance_label" in dim_cols  # the process that served the event
        or "build_commit" in dim_cols
        or named(input.user_id) is not None
        or input.traffic_class is not None
        or input.instance_label is not None
    )
    scope_where = ""
    scope_params: dict[str, Any] = {}
    if not input.namespace:
        # Org-wide query: scope to the caller's namespaces unless they're a
        # platform / org admin (the Loom operator). Without this, a regular
        # user lands on the Usage tab and sees the entire org's LLM spend.
        from server.auth.context import resolve_org_scope

        scope = await resolve_org_scope(input.org)
        if not scope.accessible:
            return CostSummaryOutput(rows=[], total_ops=0, total_errors=0, total_cost_usd=0.0)
        scope_where, scope_params = scope.where, dict(scope.params)
    where, params = cost_store.summary_where(
        input.org,
        input.from_date,
        input.to_date,
        namespace=input.namespace,
        scope_where=scope_where,
        scope_params=scope_params,
        filters={
            "agent": input.agent,
            "operation": input.operation,
            "user_id": named(input.user_id),
            "traffic_class": input.traffic_class,
            "instance_label": input.instance_label,
        },
    )
    raw_rows = await cost_store.summary_rows(
        where,
        params,
        dims=dim_cols,
        scan=needs_events_scan,
        include_cache=input.include_cache,
        ch=ch,
    )

    cache_hits_total = 0
    cache_saved_total = 0.0
    if input.include_cache and not needs_events_scan and input.operation in (None, "memory.ask"):
        cache_hits_total, cache_saved_total = await cost_store.ask_cache_totals(
            where, params, ch=ch
        )

    rows: list[CostSummaryRow] = []
    total_ops = 0
    total_errors = 0
    total_cost = 0.0
    for r in raw_rows:
        row = _summary_row(r, scan=needs_events_scan)
        rows.append(row)
        total_ops += row.ops
        total_errors += row.errors
        total_cost += row.cost_usd
        if needs_events_scan:
            cache_hits_total += row.cache_hits
            cache_saved_total += row.cache_saved_usd

    return CostSummaryOutput(
        rows=rows,
        total_ops=total_ops,
        total_errors=total_errors,
        total_cost_usd=total_cost,
        cache_hits=cache_hits_total,
        cache_saved_usd=round(cache_saved_total, 6),
    )


async def handle_budget_status(input: BudgetStatusInput) -> BudgetStatusOutput:
    """Compute current 24h spend + effective cap.

    namespace given → per-namespace cap evaluation (used by worker
    guards + per-namespace Usage bar).
    namespace omitted → org-wide rollup (admin visibility into "what's
    this tenant spending across all namespaces").
    """
    ch = get_client()

    if input.namespace:
        spent = await cost_store.namespace_spend_24h(input.org, input.namespace, ch=ch)
        cap, source = effective_cap(
            await cost_store.namespace_cap(input.org, input.namespace, ch=ch)
        )
        return BudgetStatusOutput(
            namespace=input.namespace,
            spent_24h_usd=round(spent, 6),
            cap_usd=round(cap, 4),
            cap_source=source,
            over_cap=(cap > 0 and spent >= cap),
            pct_of_cap=(round(min(1.0, spent / cap), 4) if cap > 0 else None),
        )

    # Org-wide rollup. Same scoping as cost.summary: a regular user
    # without ORG_ADMIN sees only their accessible namespaces; a
    # platform/org admin (the Loom operator) sees everything.
    from server.auth.context import resolve_org_scope

    org_scope = await resolve_org_scope(input.org)
    if not org_scope.accessible:
        return BudgetStatusOutput(
            namespace="",
            spent_24h_usd=0.0,
            cap_usd=0.0,
            cap_source="org",
            over_cap=False,
            pct_of_cap=None,
            namespaces_with_spend=0,
        )
    spent, ns_count = await cost_store.org_spend_24h(
        input.org, org_scope.where, dict(org_scope.params), ch=ch
    )
    return BudgetStatusOutput(
        namespace="",
        spent_24h_usd=round(spent, 6),
        cap_usd=0.0,
        cap_source="org",
        over_cap=False,
        pct_of_cap=None,
        namespaces_with_spend=ns_count,
    )


def _parse_turn_at(text: str) -> datetime | None:
    """A turn's ISO-8601 time in UTC, or None when it has none, does not parse,
    or its offset takes it before year 1 or after 9999."""
    try:
        parsed = datetime.fromisoformat(text)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


async def handle_cost_record_turns(input: CostRecordTurnsInput) -> CostRecordTurnsOutput:
    """Price each turn from the shared table and write the ledger rows.
    Best-effort write: a CH failure records nothing rather than raising —
    the hook that calls this has already finished the user's turn."""
    from server.observability.cost_accumulator import is_priced, turn_cost_usd

    rows: list[list[Any]] = []
    unpriced: list[str] = []
    priced_n = 0
    total = 0.0
    now = datetime.now(UTC)
    ahead = 0
    parsed = [_parse_turn_at(t.turn_at) for t in input.turns]
    # A turn sent without a time keeps the one it is recorded with, so a
    # replay lands in the same run.
    undated = [t.message_id for t, at in zip(input.turns, parsed, strict=True) if at is None]
    recorded: dict[str, datetime] = {}
    if undated:
        try:
            recorded = await cost_store.recorded_turn_times(
                input.org, input.namespace, input.session_id, undated, ch=get_client()
            )
        except Exception:  # a failed lookup dates the turns now; the turn still records
            log.warning(
                "cost.record_turns: turn time lookup failed; undated turns dated now", exc_info=True
            )
    for t, turn_at in zip(input.turns, parsed, strict=True):
        cost = turn_cost_usd(
            t.model,
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cache_read_tokens=t.cache_read_tokens,
            cache_creation_5m_tokens=t.cache_creation_5m_tokens,
            cache_creation_1h_tokens=t.cache_creation_1h_tokens,
        )
        priced = is_priced(t.model)
        if priced:
            priced_n += 1
            total += cost
        elif t.model not in unpriced:
            unpriced.append(t.model)
        if turn_at is None:
            turn_at = recorded.get(t.message_id, now)
        elif turn_at > now + _AHEAD_SLACK:
            # Kept as sent, so a replay lands in the same run; a client clock
            # running ahead is reported instead.
            ahead += 1
        rows.append(
            [
                input.org,
                input.namespace,
                input.agent or "-",
                input.user_id or "-",
                input.session_id,
                t.message_id,
                input.consumer,
                t.model,
                t.input_tokens,
                t.cache_read_tokens,
                t.cache_creation_5m_tokens,
                t.cache_creation_1h_tokens,
                t.output_tokens,
                round(cost if priced else 0.0, 6),
                priced,
                turn_at,  # aware UTC: the driver reads a naive one as local
            ]
        )
    try:
        await cost_store.insert_turn_usage(rows, ch=get_client())
    except Exception:  # noqa: BLE001 — a spend ledger must never fail the turn
        return CostRecordTurnsOutput(recorded=0, priced=0, unpriced_models=unpriced, cost_usd=0.0)
    return CostRecordTurnsOutput(
        recorded=len(rows),
        priced=priced_n,
        unpriced_models=unpriced,
        cost_usd=round(total, 6),
        turns_dated_ahead=ahead,
    )


async def handle_cost_agent_summary(input: CostAgentSummaryInput) -> CostAgentSummaryOutput:
    """Rollup of loom.agent_turn_usage (FINAL — the ledger is a
    ReplacingMergeTree keyed on session + message). Same org scoping as
    cost.summary: an org-wide call sees only the caller's namespaces unless
    they are an operator."""

    dims = list(input.group_by)
    scope_where = ""
    scope_params: dict[str, Any] = {}
    if not input.namespace:
        from server.auth.context import resolve_org_scope

        scope = await resolve_org_scope(input.org)
        if not scope.accessible:
            return CostAgentSummaryOutput(
                rows=[], total_turns=0, total_sessions=0, total_cost_usd=0.0, unpriced_turns=0
            )
        scope_where, scope_params = scope.where, dict(scope.params)
    raw = await cost_store.agent_summary_rows(
        input.org,
        input.from_date,
        input.to_date,
        dims=dims,
        namespace=input.namespace,
        scope_where=scope_where,
        scope_params=scope_params,
        agent=input.agent,
        consumer=input.consumer,
        ch=get_client(),
    )
    rows: list[CostAgentSummaryRow] = []
    total_turns = unpriced = sessions_seen = 0
    total_cost = 0.0
    for r in raw:
        row = CostAgentSummaryRow(
            namespace=r.get("namespace") if "namespace" in dims else None,
            agent=r.get("agent") if "agent" in dims else None,
            day=r.get("day") if "day" in dims else None,
            model=r.get("model") if "model" in dims else None,
            consumer=r.get("consumer") if "consumer" in dims else None,
            session_id=r.get("session_id") if "session_id" in dims else None,
            turns=int(r.get("turns") or 0),
            sessions=int(r.get("sessions") or 0),
            input_tokens=int(r.get("input_tokens") or 0),
            cache_read_tokens=int(r.get("cache_read_tokens") or 0),
            cache_write_tokens=int(r.get("cache_write_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            cost_usd=round(float(r.get("cost_usd") or 0.0), 6),
            unpriced_turns=int(r.get("unpriced_turns") or 0),
        )
        rows.append(row)
        total_turns += row.turns
        total_cost += row.cost_usd
        unpriced += row.unpriced_turns
        if "session_id" in dims:
            sessions_seen += row.sessions
        else:
            sessions_seen = max(sessions_seen, row.sessions)
    return CostAgentSummaryOutput(
        rows=rows,
        total_turns=total_turns,
        total_sessions=sessions_seen,
        total_cost_usd=round(total_cost, 6),
        unpriced_turns=unpriced,
    )
