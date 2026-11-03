"""Who read what: memory.exposure_summary (an operator view of the exposure
ledger) and memory.owner_report (an owner's view of what they own, who read it
and how it was used). Wire models in server/memory/api/models_exposure.py."""

from __future__ import annotations

from typing import Any

from server.memory.api.models_exposure import (
    ExposureSummaryInput,
    ExposureSummaryOutput,
    MemoryOwnerReportInput,
    MemoryOwnerReportOutput,
    _ExposureSummaryRow,
    _OwnerReadsRow,
    _OwnerRowsRow,
    _OwnerUsageRow,
)


async def handle_memory_exposure_summary(input: ExposureSummaryInput) -> ExposureSummaryOutput:
    from server.memory.app.exposure_ledger import summarize

    rows, total = await summarize(
        input.org,
        input.namespace,
        since_hours=input.since_hours,
        limit=input.limit,
    )
    return ExposureSummaryOutput(
        namespace_rows=total,
        rows=[
            _ExposureSummaryRow(
                actor=str(r.get("actor") or ""),
                agent=str(r.get("agent") or ""),
                distinct_keys=int(r.get("distinct_keys") or 0),
                calls=int(r.get("calls") or 0),
                fraction=(int(r.get("distinct_keys") or 0) / total) if total else 0.0,
                first_hour=r["first_hour"],
                last_hour=r["last_hour"],
            )
            for r in rows
        ],
    )


async def handle_memory_owner_report(input: MemoryOwnerReportInput) -> MemoryOwnerReportOutput:
    from server.auth.authz import row_in_scope
    from server.auth.context import (
        current_auth,
        every_readable_namespace,
        rollup_member_namespaces,
    )
    from server.identity.domain.read_scope import resolve_read_filters
    from server.memory.app.owner_report import NamespaceScope, namespace_scope, owner_report
    from server.operations.base import Role

    auth = current_auth()
    rf = resolve_read_filters(
        requested="own_user", user_id=input.user_id, session_id=None, auth=auth
    )
    uid = str(rf.user_id or "")
    empty = MemoryOwnerReportOutput(
        user_id=uid, since_hours=input.since_hours, live_rows=0, rows=[], reads=[], usage=[]
    )
    if input.namespace:
        if not await row_in_scope(input.org, input.namespace, Role.VIEWER):
            raise PermissionError(f"namespace {input.namespace!r} is not readable by this token")
        scope = namespace_scope([input.namespace])
    elif auth is None or ("*" in auth.namespaces and not auth.has_ns_roles()):
        # Auth off, or a namespace-wildcard token: every namespace of the org
        # but the restricted ones and collections it may not read (a `*` never
        # counts toward those).
        every = await every_readable_namespace(input.org)
        if not every.accessible:
            return empty
        every_e = await every_readable_namespace(input.org, column="e.namespace")
        scope = NamespaceScope(every.where, every_e.where, every.params)
    else:
        # The claim plus grants (a scoped token: its claim alone), less the
        # restricted namespaces it may not read. Empty is "none", never "all".
        readable = await rollup_member_namespaces(input.org)
        if not readable:
            return empty
        scope = namespace_scope(readable)
    rep = await owner_report(
        org=input.org,
        user_id=uid,
        scope=scope,
        since_hours=input.since_hours,
        limit=input.limit,
    )
    rows = [_OwnerRowsRow(**r) for r in rep["rows"]]
    return MemoryOwnerReportOutput(
        user_id=uid,
        since_hours=input.since_hours,
        live_rows=sum(r.n for r in rows),
        rows=rows,
        reads=[_OwnerReadsRow(**r) for r in rep["reads"]],
        usage=[_usage_row(r) for r in rep["usage"]],
    )


def _usage_row(r: dict[str, Any]) -> _OwnerUsageRow:
    return _OwnerUsageRow(
        namespace=str(r.get("namespace") or ""),
        recalls=int(r.get("recalls") or 0),
        used=int(r.get("used") or 0),
        harm=int(r.get("harm") or 0),
        last_recalled=r.get("last_recalled"),
    )
