"""Admin endpoints — every `/v1/admin/*` operator endpoint, mounted via
`app.include_router(admin_router)` in main.py.

SQL lives behind `server.insights.store.admin` (and other repositories for
cross-cutting tables); handlers are dispatch+adapt only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from server.core.ch import set_query_tag
from server.core.paths import MIGRATIONS_DIR
from server.erasure.app.backends import ErasureBackendError, LangfuseBackend
from server.insights.app.admin_models import (
    AuditSummaryInput,
    AuditSummaryOutput,
    BudgetSetInput,
    BudgetSetOutput,
    BudgetViolationsInput,
    BudgetViolationsOutput,
    ConsumerRow,
    ConsumersInput,
    ConsumersOutput,
    ConsumersWindow,
    ConsumerTaggingRow,
    EventsTailInput,
    EventsTailOutput,
    FleetCostInput,
    FleetCostOutput,
    NamespaceAdoptInput,
    PromptsEvaluateInput,
    PromptsEvaluateOutput,
    RatelimitSetInput,
    RatelimitSetOutput,
    SchemaStatusOutput,
    SettingChange,
    SettingRow,
    SettingSetInput,
    SettingSetOutput,
    SettingsHistoryInput,
    SettingsHistoryOutput,
    SettingsListInput,
    SettingsListOutput,
    TokenMintInput,
    TokenMintOutput,
    WorkersStatusOutput,
)
from server.insights.store import admin as admin_repo
from server.observability import audit
from server.operations.base import Role
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.delegation import Delegated
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)


async def require_admin(request: Request) -> AuthInfo | None:
    """Bearer + PLATFORM_ADMIN gate for the deployment-wide admin endpoints.

    Admin routes never pass through the operation-registry transport (and its
    auth step), so the check has to live on these routers. Returns the
    verified principal, or None when the effective auth mode is off — the
    same `auth_actually_required()` the transport reads, so a `single-user`
    identity provider over a prod-derived `LOOM_AUTH_REQUIRED=true` does not
    lock the admin surface while every other route is open.
    """
    return _verified_admin(request, Role.PLATFORM_ADMIN)


async def require_org_admin(request: Request) -> AuthInfo | None:
    """Bearer + ORG_ADMIN gate for the endpoints that act inside the one org
    their body names; each handler pins that org to the caller's own (a
    platform admin passes for any org)."""
    return _verified_admin(request, Role.ORG_ADMIN)


def _verified_admin(request: Request, needed: Role) -> AuthInfo | None:
    """A per-namespace admin (a flat `admin` role, or a scoped token's
    `ns_roles`) is not deployment or org authority and never passes."""
    from server.auth.bearer import missing_bearer, verify_bearer_header

    if not settings.auth_actually_required():
        return None
    info = verify_bearer_header(request.headers.get("Authorization"))
    if info is None:
        raise missing_bearer()
    if info.has_ns_roles() or not info.has_role(needed):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_INSUFFICIENT",
                "message": f"this admin endpoint requires the {needed.name} role",
            },
        )
    return info


# Router-level dependencies run the gate before every handler, so no admin
# endpoint can ship unauthenticated by omission. A new endpoint goes on
# `admin_router` (deployment-wide) unless it acts inside one org only.
admin_router = APIRouter(dependencies=[Depends(require_admin)])
org_admin_router = APIRouter(dependencies=[Depends(require_org_admin)])


# ============================================================
# Schema + worker status
# ============================================================


@admin_router.get("/v1/admin/schema.status")
async def admin_schema_status() -> SchemaStatusOutput:
    """Report the gap between what migrations expect and what's in CH.

    expected = what a replay of the migrations leaves (creates less drops);
    missing = expected ∖ present (run `make migrate`);
    extra = present ∖ expected (stale tables, drift, or MV inner-ids).
    """

    from server.core.schema.journal import live_objects
    from server.core.schema.migrate_lease import RUNNER_TABLES

    expected = live_objects(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else set()
    expected |= RUNNER_TABLES

    # Skip internal MV inner_id.* tables — repo helper filters them.
    present = set(await admin_repo.list_loom_tables())
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    return SchemaStatusOutput.model_validate(
        {
            "expected": sorted(expected),
            "present": sorted(present),
            "missing": missing,
            "extra": extra,
            "drift": bool(missing or extra),
        }
    )


@admin_router.get("/v1/admin/workers.status")
async def admin_workers_status() -> WorkersStatusOutput:
    """Snapshot of each background worker's config + most recent run summary.

    The union of this process's registry and the latest `loom.worker_status` row per worker
    from ClickHouse, freshest report first — under blue/green the web tier runs no worker, so
    without the store this read would say "no workers" while the workers colour runs them all.
    `store` says what the table contributed.
    """
    from server.workers import status_store
    from server.workers.registry import worker_meta
    from server.workers.status import get_registry

    snapshot = get_registry().snapshot()
    store_rows = await status_store.latest_rows()

    # Decorate every known worker with its config even if it has never run, so
    # callers can distinguish "disabled" from "enabled but hasn't fired yet."
    out: dict[str, dict[str, object]] = {}
    for name, m in worker_meta().items():
        enabled = bool(m["enabled"])
        reason = None if enabled else "disabled_by_setting"
        # off by its own LOOM_*_ENABLED switch or by the plane switch it also requires
        detail = None if enabled else "A setting turned this worker off, or the plane it needs."
        base: dict[str, object] = {
            "enabled": enabled,
            "effective": enabled,
            "interval_seconds": m["interval_seconds"],
            "idle_interval_seconds": m["idle_interval_seconds"],
            "disabled_reason": reason,
            "disabled_detail": detail,
        }
        out[name] = {**base, **snapshot.get(name, {})}
    merged = status_store.merge_status(out, store_rows)
    return WorkersStatusOutput.model_validate(
        {
            "workers": merged,
            "store": {
                "table": status_store.TABLE,
                "rows": len(store_rows),
                "reporters": sorted({str(r.get("instance_label") or "") for r in store_rows}),
                "in_process": sum(1 for w in merged.values() if w.get("source") == "registry"),
                "from_store": sum(1 for w in merged.values() if w.get("source") == "store"),
            },
        }
    )


# ============================================================
# Audit / events tail
# ============================================================


@admin_router.post("/v1/admin/audit.summary")
async def admin_audit_summary(input: AuditSummaryInput) -> AuditSummaryOutput:
    """Aggregate audit_log by event_type in a window."""
    rows = await admin_repo.audit_summary(
        input.since_seconds,
        org=input.org,
        namespace=input.namespace,
    )
    total = sum(int(r["cnt"]) for r in rows)
    return AuditSummaryOutput.model_validate(
        {
            "rows": [
                {
                    "event_type": r["event_type"],
                    "count": int(r["cnt"]),
                    "first_ts": r["first_ts"].isoformat(),
                    "last_ts": r["last_ts"].isoformat(),
                }
                for r in rows
            ],
            "total": total,
            "since_seconds": input.since_seconds,
        }
    )


@admin_router.post("/v1/admin/events.tail")
async def admin_events_tail(input: EventsTailInput) -> EventsTailOutput:
    """Recent rows from `loom.agent_events` for live debugging."""
    filters: dict[str, str | bool] = {}
    for key in ("org", "namespace", "agent", "operation"):
        val = getattr(input, key)
        if val:
            filters[key] = val
    if input.success is not None:
        filters["success"] = input.success
    rows = await admin_repo.events_tail(
        input.since_seconds,
        input.limit,
        filters=filters,
    )
    return EventsTailOutput.model_validate(
        {
            "rows": [
                {
                    "ts": r["ts"].isoformat(),
                    "org": r["org"],
                    "namespace": r["namespace"],
                    "agent": r["agent"],
                    "operation": r["operation"],
                    "latency_ms": int(r["latency_ms"]),
                    "success": bool(r["success"]),
                    "error_code": r["error_code"],
                    "request_id": r["request_id"],
                    "trace_id": r["trace_id"],
                }
                for r in rows
            ],
            "since_seconds": input.since_seconds,
        }
    )


# ============================================================
# Budget + ratelimit admin
# ============================================================


@admin_router.post("/v1/admin/budget.violations")
async def admin_budget_violations(
    input: BudgetViolationsInput,
) -> BudgetViolationsOutput:
    """Aggregate recent `budget.violated` events (loom.events)
    for ops alerting."""
    since_seconds = input.since_seconds
    rows = await admin_repo.budget_violations(
        since_seconds,
        input.limit,
        org=input.org,
    )

    total = sum(int(r["violations"]) for r in rows)
    out_rows = [
        {
            "namespace": r["namespace"],
            "operation": r["operation"],
            "violations": int(r["violations"]),
            "max_overage_ratio": float(r["max_overage_ratio"]),
            "avg_overage_ratio": float(r["avg_overage_ratio"]),
            "last_violation_at": (
                r["last_violation_at"].isoformat() if r["last_violation_at"] else None
            ),
        }
        for r in rows
    ]
    return BudgetViolationsOutput.model_validate(
        {
            "rows": out_rows,
            "since_seconds": since_seconds,
            "total_violations": total,
        }
    )


_CLASSES = frozenset({"service-account", "user", "anonymous"})


def _attribution_label(name: str, calls: int, claimed: int) -> str:
    """`class` for a subject-class bucket; else `claimed` when
    every call's tag was bound by a token claim, `self-reported` when none
    was, `mixed` otherwise."""
    if name in _CLASSES:
        return "class"
    if calls > 0 and claimed >= calls:
        return "claimed"
    if claimed == 0:
        return "self-reported"
    return "mixed"


@admin_router.post("/v1/admin/consumers")
async def admin_consumers(input: ConsumersInput) -> ConsumersOutput:
    """Per-consumer calls, error rate, p50, spend and outcome feedback over
    each requested window — read-only aggregates over
    agent_events and code_search_outcome, joined by consumer name."""
    from server.observability.budgets import get_overrides

    windows: list[ConsumersWindow] = []
    for since in input.windows_seconds:
        rows, outcomes = await asyncio.gather(
            admin_repo.consumers(since, input.limit, org=input.org),
            admin_repo.consumer_outcomes(since, input.limit, org=input.org),
        )
        # Tagged / untagged per token identity; additive, so a
        # failed read leaves the window's rows intact.
        tagging_rows: list[dict[str, Any]] = []
        try:
            tagging_rows = await admin_repo.consumer_tagging(since, input.limit, org=input.org)
        except Exception as e:  # noqa: BLE001 — the split is additive
            log.warning("admin/consumers: tagging read failed (non-fatal): %s", e)
        by_name: dict[str, dict[str, Any]] = {}
        for r in rows:
            name = str(r.get("consumer_key") or "")
            last = r.get("last_seen")
            _calls = int(r["calls"])
            _claimed = int(r.get("claimed_calls") or 0)
            by_name[name] = {
                "consumer": name,
                "claimed_calls": _claimed,
                "claimed_spend_usd": float(r.get("claimed_spend_usd") or 0.0),
                "attribution": _attribution_label(name, _calls, _claimed),
                "calls": _calls,
                "errors": int(r["errors"]),
                "server_errors": int(r.get("server_errors") or 0),
                "error_rate": float(r["error_rate"] or 0.0),
                "p50_ms": float(r["p50_ms"] or 0.0),
                "p95_ms": float(r["p95_ms"] or 0.0),
                "spend_usd": float(r["spend_usd"] or 0.0),
                "outcome_calls": int(r.get("outcome_calls") or 0),
                "namespaces": int(r.get("namespaces") or 0),
                "ops_distinct": int(r.get("ops_distinct") or 0),
                "last_seen": (
                    last.isoformat()
                    if isinstance(last, datetime)
                    else (str(last) if last else None)
                ),
            }
        for o in outcomes:
            name = str(o.get("consumer_key") or "")
            row = by_name.setdefault(
                name,
                {
                    "consumer": name,
                    "calls": 0,
                    "errors": 0,
                    "server_errors": 0,
                    "error_rate": 0.0,
                    "p50_ms": 0.0,
                    "p95_ms": 0.0,
                    "spend_usd": 0.0,
                    "outcome_calls": 0,
                    "namespaces": 0,
                    "ops_distinct": 0,
                    "last_seen": None,
                },
            )
            row.update(
                outcome_rows=int(o.get("outcome_rows") or 0),
                outcome_positive=int(o.get("outcome_positive") or 0),
                outcome_negative=int(o.get("outcome_negative") or 0),
                shunt_rows=int(o.get("shunt_rows") or 0),
                tokens_saved=int(o.get("tokens_saved") or 0),
            )
        tagged = sum(
            float(r["spend_usd"]) for r in by_name.values() if r["consumer"] not in _CLASSES
        )
        bound = sum(float(r.get("claimed_spend_usd") or 0.0) for r in by_name.values())
        windows.append(
            ConsumersWindow(
                since_seconds=since,
                rows=[
                    ConsumerRow(**r)
                    for r in sorted(by_name.values(), key=lambda r: (-r["calls"], r["consumer"]))
                ],
                tagging=[
                    ConsumerTaggingRow(
                        org=str(t.get("org") or ""),
                        agent=str(t.get("agent") or ""),
                        calls=int(t.get("calls") or 0),
                        tagged=int(t.get("tagged") or 0),
                        untagged=int(t.get("untagged") or 0),
                        tagged_ratio=float(t.get("tagged_ratio") or 0.0),
                        tag=str(t.get("tag") or ""),
                        tagged_ops=int(t.get("tagged_ops") or 0),
                        untagged_ops=int(t.get("untagged_ops") or 0),
                    )
                    for t in (tagging_rows or [])
                ],
                tagged_spend_usd=round(tagged, 6),
                claim_bound_spend_usd=round(bound, 6),
                claim_bound_share=round(bound / tagged, 4) if tagged > 0 else 0.0,
            )
        )
    rules: dict[str, dict[str, int | float]] = {}
    rules.update(get_overrides().settings_map())
    rules.update(get_overrides().snapshot_by_consumer())
    return ConsumersOutput(windows=windows, budget_overrides_by_consumer=rules)


@admin_router.post("/v1/admin/budget.set")
async def admin_budget_set(input: BudgetSetInput) -> BudgetSetOutput:
    """Install or clear a per-operation latency-budget override.

    Body: {op, budget_ms}  — install  /  {op, clear: true}  — remove.
    With `consumer`: {consumer, op | "*", budget_ms | budget: "3x"}
    installs for that consumer only; {consumer, op, clear: true} removes it.
    """
    from server.core import runtime_overrides as ro
    from server.core.numbers import parse_budget_value
    from server.observability.budgets import get_overrides, stored_ms

    set_query_tag("admin.budget_set")
    overrides = get_overrides()
    if input.consumer:
        if input.clear:
            await ro.clear(ro.BUDGET_CONSUMER, (input.consumer, input.op or "*"))
            overrides.clear_for_consumer(input.consumer, input.op)
            return BudgetSetOutput(ok=True, cleared=True, op=input.op, consumer=input.consumer)
        value: int | str | None = input.budget if input.budget is not None else input.budget_ms
        if value is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": "budget_ms (ms) or budget ('<n>x') required when clear=false",
                },
            )
        if parse_budget_value(value) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": "budget must be a non-negative integer (ms) or '<n>x'",
                },
            )
        # Stored first: a failed write changes no process.
        await ro.put(ro.BUDGET_CONSUMER, (input.consumer, input.op or "*"), value)
        overrides.set_for_consumer(input.consumer, input.op, value)
        return BudgetSetOutput(
            ok=True,
            op=input.op,
            consumer=input.consumer,
            budget=value,
            budget_ms=value if isinstance(value, int) else None,
        )
    if input.clear:
        await ro.clear(ro.BUDGET_OP, (input.op,))
        overrides.clear(input.op)
        return BudgetSetOutput.model_validate({"ok": True, "cleared": True, "op": input.op})
    if input.budget_ms is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "budget_ms required when clear=false",
            },
        )
    try:
        stored_ms(input.budget_ms)  # refused before it is stored
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"code": "VALIDATION_FAILED", "message": str(e)}
        ) from e
    await ro.put(ro.BUDGET_OP, (input.op,), input.budget_ms)
    overrides.set(input.op, input.budget_ms)
    return BudgetSetOutput.model_validate(
        {"ok": True, "op": input.op, "budget_ms": input.budget_ms}
    )


@admin_router.post("/v1/admin/ratelimit.set")
async def admin_ratelimit_set(input: RatelimitSetInput) -> RatelimitSetOutput:
    """Install a per-(namespace|org) rate-limit override.

    Body shapes (exactly one of namespace / org, plus op_class):
      {namespace, op_class, capacity, rate}     — install
      {namespace, op_class, clear: true}        — remove
      {org,       op_class, capacity, rate}     — install (org-tier)
      {org,       op_class, clear: true}        — remove (org-tier)
    """
    from server.core import runtime_overrides as ro
    from server.core.ratelimit import Limit, get_limiter

    set_query_tag("admin.ratelimit_set")
    ns = input.namespace or ""
    org = input.org or ""
    if bool(ns) == bool(org):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "exactly one of `namespace` or `org` required",
            },
        )
    limiter = get_limiter()
    if input.clear:
        if ns:
            await ro.clear(ro.RATELIMIT_NAMESPACE, (ns, input.op_class))
            limiter.clear_namespace_limit(ns, input.op_class)
            return RatelimitSetOutput.model_validate(
                {
                    "ok": True,
                    "cleared": True,
                    "scope": "namespace",
                    "key": ns,
                    "op_class": input.op_class,
                }
            )
        await ro.clear(ro.RATELIMIT_ORG, (org, input.op_class))
        limiter.clear_org_limit(org, input.op_class)
        return RatelimitSetOutput.model_validate(
            {
                "ok": True,
                "cleared": True,
                "scope": "org",
                "key": org,
                "op_class": input.op_class,
            }
        )
    if input.capacity is None or input.rate is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "capacity and rate required when clear=false",
            },
        )
    value = {"capacity": input.capacity, "rate": input.rate}
    if ns:
        await ro.put(ro.RATELIMIT_NAMESPACE, (ns, input.op_class), value)
        limiter.set_namespace_limit(
            ns,
            input.op_class,
            Limit(capacity=input.capacity, rate=input.rate),
        )
        return RatelimitSetOutput.model_validate(
            {
                "ok": True,
                "scope": "namespace",
                "key": ns,
                "op_class": input.op_class,
                "capacity": input.capacity,
                "rate": input.rate,
            }
        )
    await ro.put(ro.RATELIMIT_ORG, (org, input.op_class), value)
    limiter.set_org_limit(
        org,
        input.op_class,
        Limit(capacity=input.capacity, rate=input.rate),
    )
    return RatelimitSetOutput.model_validate(
        {
            "ok": True,
            "scope": "org",
            "key": org,
            "op_class": input.op_class,
            "capacity": input.capacity,
            "rate": input.rate,
        }
    )


@admin_router.post("/v1/admin/settings.list")
async def admin_settings_list(input: SettingsListInput) -> SettingsListOutput:
    """Every setting with its value in force in this process, its default, where
    the value came from (override, dashboard, env, env_file, settings_file,
    inferred, default) and whether it is live."""
    from server.core import settings_overrides

    rows = settings_overrides.rows(prefix=input.prefix, changed=input.changed)
    return SettingsListOutput(settings=[SettingRow(**r) for r in rows])


@admin_router.post("/v1/admin/settings.set")
async def admin_settings_set(
    request: Request,
    input: SettingSetInput,
    # The router's own gate, cached for the request: the token is verified once.
    info: Annotated[Any, Depends(require_admin)],
) -> SettingSetOutput:
    """Install a deployment-wide override of a live setting ({name, value}) or
    remove it ({name, clear: true}); with `expected`, only while that is the value
    in force. Every process applies it within `runtime_overrides_refresh_seconds`;
    this one at once."""
    from server.core.settings_overrides import NOT_GIVEN
    from server.insights.app import settings_changes

    set_query_tag("admin.settings_set")
    refuse_browser_reach(request, "setting changes")
    row = await settings_changes.change(
        input.name,
        input.value,
        clear=input.clear,
        expected=input.expected if "expected" in input.model_fields_set else NOT_GIVEN,
        actor=info.subject if info is not None else "dev-admin",
    )
    return SettingSetOutput(ok=True, setting=SettingRow(**row) if row else None)


@admin_router.post("/v1/admin/settings.history")
async def admin_settings_history(input: SettingsHistoryInput) -> SettingsHistoryOutput:
    """The recorded changes of one setting, or of every setting, newest first: who
    made each, the value before and after. Undo one by setting `previous` back when
    `previous_source` is `override`, by a reset when it is another source; a null one
    (the reset of a setting this build lacks) has no undo."""
    from server.insights.app import settings_changes

    rows = await settings_changes.history(input.name, input.limit)
    return SettingsHistoryOutput(changes=[SettingChange(**r) for r in rows])


# ============================================================
# Fleet observability — aggregate views over agent_events / audit_log
# ============================================================


@admin_router.post("/v1/admin/fleet.cost")
async def admin_fleet_cost(input: FleetCostInput) -> FleetCostOutput:
    """Top-N tenants by attributed cost over a window."""
    since_seconds = input.since_seconds
    top_n = input.top_n
    group_by = input.group_by
    rows = await admin_repo.fleet_cost(since_seconds, top_n, group_by)
    return FleetCostOutput.model_validate(
        {
            "rows": [
                {
                    "key": r["key"],
                    "total_cost_usd": float(r["total_cost_usd"] or 0.0),
                    "total_calls": int(r["total_calls"]),
                    "ops_distinct": int(r["ops_distinct"]),
                    **(
                        {"namespaces_distinct": int(r["namespaces_distinct"])}
                        if group_by != "namespace"
                        else {}
                    ),
                }
                for r in rows
            ],
            "since_seconds": since_seconds,
            "group_by": group_by,
            "top_n": top_n,
        }
    )


# ============================================================
# Prompt evaluation gate (Langfuse-backed)
# ============================================================


# The traces one evaluation reads, and the scores it reads per trace.
_EVAL_TRACES = 200
_EVAL_SCORES_PER_TRACE = 10


async def _rubric_scores(
    c: httpx.AsyncClient, backend: LangfuseBackend, trace_ids: list[str], rubric: str
) -> list[float]:
    """The numeric `rubric` scores on each trace. Langfuse 4 serves the score
    list only under `/api/public/v3/scores`. A failed read raises, so the caller
    answers 502 rather than reporting that no scores exist."""
    path = "/api/public/v3/scores" if await backend.api_major(c) >= 4 else "/api/public/scores"
    scores: list[float] = []
    for tid in trace_ids:
        r = await backend.get_retried(
            c, path, {"traceId": tid, "name": rubric, "limit": _EVAL_SCORES_PER_TRACE}
        )
        r.raise_for_status()
        for row in r.json().get("data") or []:
            try:
                scores.append(float(row.get("value")))
            except (TypeError, ValueError):
                pass
    return scores


@admin_router.post("/v1/admin/prompts.evaluate")
async def admin_prompts_evaluate(input: PromptsEvaluateInput) -> PromptsEvaluateOutput:
    """Compute a should-promote verdict for one prompt version.

    Finds the Langfuse traces tagged `prompt:<name>:<version>` (each generation
    linked to that prompt version carries the tag) and applies the
    (min_evals, threshold) gate to their `rubric` scores. Never mutates Langfuse.
    """
    name = input.prompt_name
    version = input.prompt_version
    rubric = input.rubric
    min_evals = input.min_evals
    threshold = input.threshold

    if not settings.langfuse_url or not settings.langfuse_admin_token:
        # 501 not 503: integration disabled, not down — clients should not retry.
        raise HTTPException(
            status_code=501,
            detail={
                "code": "INTEGRATION_NOT_CONFIGURED",
                "message": "Langfuse not configured; prompts.evaluate "
                "requires LOOM_LANGFUSE_URL + "
                "LOOM_LANGFUSE_ADMIN_TOKEN",
            },
        )

    backend = LangfuseBackend(
        settings.langfuse_url, settings.langfuse_admin_token, settings.langfuse_timeout_s
    )
    tag = f"prompt:{name}:{version}"
    try:
        async with httpx.AsyncClient(timeout=settings.langfuse_timeout_s) as c:
            trace_ids = await backend.find_trace_ids(c, tags=[tag], first=_EVAL_TRACES)
            scores = await _rubric_scores(c, backend, trace_ids, rubric)
    except (ErasureBackendError, httpx.HTTPError, ValueError) as e:
        log.exception("prompt eval: Langfuse read failed for %s", tag)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "BACKEND_UNAVAILABLE",
                "message": f"Langfuse read failed: {type(e).__name__}: {e}",
            },
        ) from e
    if not trace_ids:
        return PromptsEvaluateOutput.model_validate(
            {
                "should_promote": False,
                "eval_count": 0,
                "mean_score": None,
                "min_score": None,
                "threshold": threshold,
                "rationale": f"no traces tagged {tag!r}; nothing to evaluate",
                "prompt_name": name,
                "prompt_version": version,
            }
        )

    if not scores:
        return PromptsEvaluateOutput.model_validate(
            {
                "should_promote": False,
                "eval_count": 0,
                "mean_score": None,
                "min_score": None,
                "threshold": threshold,
                "rationale": f"{len(trace_ids)} traces matched but no '{rubric}' scores found",
                "prompt_name": name,
                "prompt_version": version,
            }
        )

    mean = sum(scores) / len(scores)
    mn = min(scores)
    enough_evals = len(scores) >= min_evals
    score_passes = mean >= threshold
    should = enough_evals and score_passes
    if not enough_evals:
        rationale = (
            f"only {len(scores)} score(s) for rubric={rubric!r}; "
            f"need ≥ {min_evals} before promoting"
        )
    elif not score_passes:
        rationale = f"mean score {mean:.3f} below threshold {threshold:.2f} ({len(scores)} evals)"
    else:
        rationale = (
            f"{len(scores)} evals, mean {mean:.3f} ≥ threshold {threshold:.2f} "
            f"(min observed {mn:.3f}) — promote"
        )
    return PromptsEvaluateOutput.model_validate(
        {
            "should_promote": should,
            "eval_count": len(scores),
            "mean_score": mean,
            "min_score": mn,
            "threshold": threshold,
            "rationale": rationale,
            "prompt_name": name,
            "prompt_version": version,
        }
    )


# ============================================================
# Token minting (admin-gated)
# ============================================================


@org_admin_router.post("/v1/admin/token.mint")
async def admin_token_mint(
    request: Request,
    input: TokenMintInput,
) -> TokenMintOutput:
    """Mint a Loom JWT. Requires ORG_ADMIN in `input.org`; the token is
    delegated from the caller's (`server.auth.delegation`), so it never
    outranks the caller's token or live grants. It is saved to the caller's
    token list under `input.name`, with its own `sa:` subject, and revoked
    there; `loom mint-token` mints for any other subject."""
    from server.auth.delegation import DEFAULT_TTL_DAYS, NEVER, Delegated, delegable_role, delegate
    from server.auth.grant_claims import service_account_change
    from server.auth.jwt import new_jti
    from server.core.datetimes import naive_utc_now_seconds
    from server.identity.app import me_service_accounts as tokens
    from server.identity.app import token_grants
    from server.identity.app.restricted_access import note_restricted_grants

    # Redundant with the router-level dependency; kept so the gate holds even
    # if this handler is ever mounted on another router.
    info = await require_org_admin(request)
    refuse_foreign_org(info, input.org, "mint a token for")
    body = await request.json()
    if info is not None:
        creator = info.subject
    else:
        # Auth off: a valid bearer names the owner, in its own org; else "dev".
        # The token list reads the same owner, so the token is listed and revocable.
        bearer = tokens.dev_bearer(request)
        refuse_foreign_org(bearer, input.org, "mint a token for")
        creator = bearer.subject if bearer is not None else "dev"
    if input.name is None and "subject" in body:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "`subject` is retired: pass `name` (the token gets its own `sa:` "
                "subject); `loom mint-token` mints for a given subject",
            },
        )
    name = tokens.token_name(input.name)
    for role in input.roles:
        delegable_role(role)
    requested: dict[str, str | None] = {ns: input.requested_role_for(ns) for ns in input.namespaces}

    jti = new_jti()
    async with service_account_change(input.org, name):
        await tokens.refuse_taken_name(input.org, name, creator)
        if info is not None:
            delegated = await delegate(
                info,
                org=input.org,
                requested=requested,
                scoped=bool(input.ns_roles),
                ttl_seconds=input.ttl_seconds,
                is_service_account=True,
            )
            # Capped at the caller's own grant there; read before anything is saved.
            roles_on = await token_grants.restricted_roles(input.org, creator, delegated, requested)
        else:
            # Auth off: nothing to delegate from.
            ttl = DEFAULT_TTL_DAYS * 86400 if input.ttl_seconds is None else input.ttl_seconds
            delegated = Delegated(
                org=input.org,
                namespaces=list(input.namespaces),
                roles=[] if input.ns_roles else list(input.roles),
                ns_roles=dict(input.ns_roles) if input.ns_roles else None,
                write_namespaces=None,
                read_scope="",
                read_user_id="",
                ttl_seconds=None if ttl == NEVER else ttl,
                parents=[],
                parent_subjects=[],
                parent_iats=[],
                creator="",
                is_service_account=True,
            )
            # Every namespace of the token; the grant below keeps only the restricted ones.
            flat_role = (delegated.roles or ["viewer"])[0]
            roles_on = delegated.ns_roles or dict.fromkeys(delegated.namespaces, flat_role)
        # Before the token is saved: a refusal (an unread code registry) strands nothing.
        from server.identity.app.namespace_classification import is_restricted

        is_code = await token_grants.code_namespace_predicate(
            input.org, list(roles_on), is_restricted
        )
        sa_subject = tokens.new_token_subject(input.org, name, creator)
        minted = delegated.mint(sa_subject, jti=jti, consumer=input.consumer or None)
        await tokens.save_token(
            name, sa_subject, delegated, created_by=creator, now=naive_utc_now_seconds()
        )
    # A restricted namespace is read only by a subject with a direct grant there.
    granted = await token_grants.grant_restricted_to_token(
        input.org, sa_subject, creator, roles_on, is_code_namespace=is_code
    )
    await note_restricted_grants(
        info, op="admin.token.mint", org=input.org, namespaces=granted, granted_to=sa_subject
    )
    if info is not None:
        await _audit_mint(info, input, delegated, sa_subject, jti)
    # Retired: the token's subject is its own, and it is always a service account.
    ignored = sorted({"subject", "is_service_account"} & set(body))
    return TokenMintOutput.model_validate(
        {
            "token": minted,
            "jti": jti,
            "name": name,
            "subject": sa_subject,
            "org": input.org,
            "namespaces": delegated.namespaces,
            "roles": delegated.roles,
            "ns_roles": delegated.ns_roles or None,
            "consumer": input.consumer,
            "ttl_seconds": delegated.ttl_seconds,
            "ignored_args": ignored,
        }
    )


def refuse_browser_reach(request: Request, what: str) -> None:
    """Refuse a write a web page could send from the operator's browser: with auth
    off, a Host that is not loopback (DNS rebinding of a keyless local install); always,
    a cross-site Origin or Sec-Fetch-Site."""
    if not settings.auth_actually_required() and request.url.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise HTTPException(403, f"Enable authentication for remote {what}")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            raise HTTPException(403, f"Cross-origin {what} is not allowed")
    if request.headers.get("sec-fetch-site", "none") not in ("same-origin", "none"):
        raise HTTPException(403, f"Cross-origin {what} is not allowed")


def refuse_foreign_org(info: AuthInfo | None, org: str, action: str) -> None:
    """An admin token acts inside its own org; only a platform admin crosses orgs."""
    if info is not None and org != info.org and not info.has_role(Role.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_INSUFFICIENT",
                "message": f"cannot {action} org {org!r} from org {info.org!r}",
            },
        )


async def _audit_mint(
    info: AuthInfo, input: TokenMintInput, granted: Delegated, subject: str, jti: str
) -> None:
    """Records what the child was granted, not what was asked for."""
    namespaces = granted.namespaces
    await audit.emit(
        "token.minted",
        org=input.org,
        namespace=namespaces[0] if len(namespaces) == 1 else "-",
        actor=info.subject,
        subject={
            "subject": subject,
            "name": input.name,
            "jti": jti,
            "namespaces": namespaces,
            "roles": granted.roles,
            "ns_roles": granted.ns_roles,
            "consumer": input.consumer,
        },
    )


@org_admin_router.post("/v1/admin/namespace.adopt")
async def admin_namespace_adopt(
    request: Request,
    input: NamespaceAdoptInput,
) -> dict[str, object]:
    """Install a new admin on a standard namespace that has no remaining active
    admin grant: the recovery path for an orphaned one. Refuses if the namespace
    already has an active admin — use `grants.create` from that session instead.
    A collection, an unregistered name, a restricted namespace the restricted gate
    refuses the caller at admin and an unreadable classification get one 404; an
    orphaned restricted namespace is an operator's to repair."""
    # Redundant with the router-level dependency; kept because this handler
    # also needs the verified subject as the audit actor.
    info = await require_org_admin(request)
    refuse_foreign_org(info, input.org, "adopt a namespace in")
    actor = info.subject if info is not None else "dev-admin"

    from server.auth.sharing import is_collection
    from server.identity.app.grants import adopt_namespace
    from server.identity.app.namespace_classification import may_administer
    from server.identity.app.registration import namespace_row
    from server.identity.app.restricted_access import note_restricted_access

    # One 404 for every refusal below, so the answer tells no names apart.
    not_found = HTTPException(
        status_code=404,
        detail={
            "code": "NAMESPACE_NOT_FOUND",
            "message": f"{input.namespace!r} is not a namespace of org {input.org!r}",
        },
    )
    # A collection is never adopted (`adopt_namespace`); its name, which may carry
    # an end user's id, gets the same answer as a missing one.
    if is_collection(input.namespace):
        raise not_found
    row = await namespace_row(input.org, input.namespace)
    if row is None:
        raise not_found
    if info is not None and not await may_administer(info, input.org, input.namespace, row=row):
        raise not_found
    await note_restricted_access(
        info, op="admin.namespace.adopt", org=input.org, namespace=input.namespace, transport="http"
    )
    now = await adopt_namespace(input.org, input.namespace, input.new_admin, actor=actor)
    if now is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NAMESPACE_HAS_ADMIN",
                "message": (
                    f"{input.namespace!r} already has an active admin; "
                    "ask them to add the new admin via grants.create"
                ),
            },
        )
    return {
        "adopted": True,
        "org": input.org,
        "namespace": input.namespace,
        "new_admin": input.new_admin,
        "granted_by": actor,
        "granted_at": now.isoformat(),
    }


__all__ = ["admin_router", "org_admin_router"]
