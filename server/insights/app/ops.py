"""ops.* handlers: the operator health checks, the projection-failure queue
and the deployment catalogue (server/insights/api/models_ops.py has the wire models)."""

from __future__ import annotations

from typing import Any, Literal

from server.core.paths import SCHEMA_SQL
from server.insights.api.models_ops import (
    OpCapability,
    OpsCapabilitiesInput,
    OpsCapabilitiesOutput,
    OpsHealthCheck,
    OpsHealthInput,
    OpsHealthOutput,
    ProjectionFailureListInput,
    ProjectionFailureListOutput,
    ProjectionFailureResolveInput,
    ProjectionFailureResolveOutput,
    ProjectionFailureRetryInput,
    ProjectionFailureRetryOutput,
    ProjectionFailureRetryRow,
    ProjectionFailureRow,
)
from server.insights.store import ops as ops_store

# The parts check fails before inserts do. ClickHouse refuses
# an INSERT at parts_to_throw_insert (3000 by default, the Cloud ceiling too);
# a table at 90 % of it is already in trouble.
PARTS_THROW_DEFAULT = 3000
PARTS_FAIL_AT = int(PARTS_THROW_DEFAULT * 0.9)
PARTS_WARN_AT = int(PARTS_THROW_DEFAULT * 0.5)

REQUIRED_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "memory_embeddings": (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "memory_key",
        "memory_type",
        "scope",
        "content_excerpt",
        "embedding_model",
        "embedding_dim",
        "embedding",
        "memory_key_embedding",
        "memory_key_embedding_dim",
        "content_excerpt_lc",
        "content_excerpt_stem",
        "content_excerpt_lemma",
        "tags",
        "status",
        "confidence",
        "temporal_anchor",
        "valid_at",
        "reinforcement_count",
        "last_reinforced_at",
        "mentioned_at",
        "source_resource_id",
    ),
    "memory_chunks": (
        "org",
        "namespace",
        "agent",
        "session_id",
        "chunk_index",
        "content_sha256",
        "content",
        "embedding",
        "embedding_model",
        "embedding_dim",
        "parent_memory_key",
        "line_start",
        "line_end",
        "modality",
        "caption",
        "source_url",
        "source_local_path",
    ),
    "memory_chunk_sources": (
        "org",
        "namespace",
        "agent",
        "session_id",
        "chunk_index",
        "content_sha256",
        "memory_key",
        "evidence_id",
        "line_start",
        "line_end",
        "created_at",
        "tombstoned_at",
    ),
    "entity_edges": (
        "org",
        "namespace",
        "entity_a",
        "entity_b",
        "co_occur",
        "last_seen",
        "valid_from",
        "valid_to",
    ),
    "entity_canonical_map": (
        "org",
        "namespace",
        "mention_text",
        "canonical_text",
        "confidence",
        "strategy",
        "evidence_count",
        "created_at",
        "updated_at",
        "tombstoned_at",
        "version",
    ),
    "entity_triples": (
        "org",
        "namespace",
        "subject",
        "relation",
        "object",
        "co_occur",
        "last_seen",
        "confidence",
        "adapter_name",
        "valid_from",
        "valid_to",
    ),
    "derived_facts": (
        "org",
        "namespace",
        "agent",
        "user_id",
        "memory_key",
        "subject",
        "predicate",
        "object",
        "category",
        "numeric_value",
        "unit",
        "occurred_at",
        "session_id",
        "confidence",
        "created_at",
    ),
    "categories": (
        "org",
        "namespace",
        "category_id",
        "name",
        "description",
        "summary",
        "embedding",
        "embedding_model",
        "embedding_dim",
        "version",
        "created_at",
        "tombstoned_at",
    ),
    "memory_categories": (
        "org",
        "namespace",
        "memory_key",
        "category_id",
        "created_at",
        "tombstoned_at",
    ),
    "memory_evidence": (
        "evidence_id",
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "run_id",
        "source_type",
        "role",
        "actor",
        "content",
        "content_sha256",
        "source_resource_id",
        "observed_at",
        "created_at",
        "metadata",
        "tombstoned_at",
    ),
    "memory_belief_sources": (
        "org",
        "namespace",
        "memory_key",
        "memory_id",
        "evidence_id",
        "support_type",
        "char_start",
        "char_end",
        "created_at",
        "tombstoned_at",
    ),
    "working_memory": (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "run_id",
        "block_key",
        "content",
        "char_limit",
        "created_at",
        "updated_at",
        "tombstoned_at",
        "version",
    ),
    "memory_schemas": (
        "schema_id",
        "org",
        "namespace",
        "name",
        "description",
        "status",
        "version",
        "created_at",
        "created_by",
        "tombstoned_at",
    ),
    "memory_schema_fields": (
        "org",
        "namespace",
        "schema_id",
        "field_name",
        "field_type",
        "required",
        "enum_values",
        "description",
        "version",
        "created_at",
        "tombstoned_at",
    ),
    "memory_records": (
        "record_id",
        "org",
        "namespace",
        "schema_id",
        "schema_name",
        "memory_key",
        "memory_id",
        "identity_agent",
        "user_id",
        "session_id",
        "record_json",
        "status",
        "valid_at",
        "valid_to",
        "version",
        "created_at",
        "updated_at",
        "tombstoned_at",
    ),
    "memory_record_conflicts": (
        "org",
        "namespace",
        "schema_name",
        "memory_key",
        "field_name",
        "prior_value",
        "incoming_value",
        "prior_record_id",
        "incoming_record_id",
        "prior_memory_id",
        "incoming_memory_id",
        "detected_at",
        "resolved_at",
        "version",
        "tombstoned_at",
    ),
    "memory_record_fields": (
        "org",
        "namespace",
        "schema_name",
        "record_id",
        "field_name",
        "field_type",
        "value_string",
        "value_number",
        "value_bool",
        "value_state",
        "evidence_id",
        "version",
        "created_at",
        "tombstoned_at",
    ),
    "memory_projection_failures": (
        "failure_id",
        "org",
        "namespace",
        "agent",
        "projection",
        "source_operation",
        "source_resource_id",
        "memory_keys",
        "error_type",
        "error_message",
        "payload",
        "status",
        "created_at",
        "resolved_at",
    ),
}


def _schema_columns(schema: str, table: str) -> set[str]:
    """Extract column names for one `loom.<table>` block in schema.sql."""

    marker = f"-- loom.{table}\n"
    start = schema.find(marker)
    if start < 0:
        return set()
    engine = schema.find("\nENGINE =", start)
    if engine < 0:
        return set()
    block = schema[start:engine]
    columns: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("`"):
            columns.add(stripped.split("`", 2)[1])
    return columns


def _read_schema_sql() -> str:
    """Read the canonical schema dump. Sync; callers wrap in asyncio.to_thread."""

    return SCHEMA_SQL.read_text()


async def _clickhouse_parts_check(ch: Any) -> OpsHealthCheck:
    """Parts and the merges that should be shrinking them.
    A merge that failed in the last quarter hour, or a table within 10 % of
    parts_to_throw_insert, is `fail` with the exception and the repair; half
    of the limit is `warn`; a quiet store is `ok`. A read failure of
    system.parts is `fail` too — the check that cannot run is not a check.
    system.part_log is replica-local and does not exist on a fresh server
    until its first flush, so its read is guarded on its own: absent means
    "no failures seen", said in `data["part_log"]`, never a failure."""

    try:
        parts = await ops_store.active_parts_by_table(ch=ch)
        max_parts = max((int(r["parts"]) for r in parts), default=0)
        # A merge that keeps failing is the failure, the part count is its
        # symptom: merges dying on a skip index the server cannot build only
        # show up as a part-count "warn" until every INSERT is refused. Failed merges in
        # the last quarter hour, or a table within reach of parts_to_throw_insert (3000
        # by default), fail here.
        part_log = "read"
        try:
            failed = await ops_store.failed_merges_15m(ch=ch)
        except Exception as e:  # noqa: BLE001 — a fresh server has no part_log yet (UNKNOWN_TABLE 60)
            failed = []
            part_log = f"absent: {str(e)[:120]}"
        near_throw = [r for r in parts if int(r["parts"]) >= PARTS_FAIL_AT]
        status: Literal["ok", "warn", "fail"]
        if failed or near_throw:
            status = "fail"
            worst = failed[0] if failed else None
            detail = (
                f"{int(worst['failures'])} failed merges on loom.{worst['table']} in 15 min: "
                f"{str(worst['exception'])[:160]}"
                if worst
                else f"{near_throw[0]['table']} has {int(near_throw[0]['parts'])} active parts "
                f"(inserts refused at {PARTS_THROW_DEFAULT})"
            )
            repair = (
                "merges are failing: read system.part_log for the exception; a skip index the "
                "server cannot build is excluded with ALTER TABLE … MODIFY SETTING "
                "exclude_materialize_skip_indexes_on_merge; parts near the "
                "insert limit refuse writes with TOO_MANY_PARTS"
            )
        else:
            status = "ok" if max_parts < PARTS_WARN_AT else "warn"
            detail = f"max active parts per loom table = {max_parts}"
            repair = (
                f"half-way to parts_to_throw_insert ({PARTS_THROW_DEFAULT}): review insert "
                "batching / async inserts for high-part tables, and check system.part_log "
                "for merges that fail"
                if max_parts >= PARTS_WARN_AT
                else ""
            )
        return OpsHealthCheck(
            name="clickhouse_parts",
            status=status,
            detail=detail,
            data={"top_tables": parts, "failed_merges": failed, "part_log": part_log},
            repair=repair,
        )
    except Exception as e:  # noqa: BLE001 — an unreadable system.parts is a fail check, never raised
        return OpsHealthCheck(
            name="clickhouse_parts",
            status="fail",
            detail=str(e),
            repair="check system.parts permissions",
        )


async def handle_ops_health(input: OpsHealthInput) -> OpsHealthOutput:
    """Run the deployment's self-checks (schema contracts, embedding stubs, ClickHouse parts,
    op mix, procedure plane, workers) and report each as ok, warn or fail."""
    import asyncio

    from server.core.ch import get_client
    from server.memory.app.snapshot_rows import persisted_columns
    from server.workers.status import get_registry

    ch = get_client()
    checks: list[OpsHealthCheck] = []

    # Schema contract: canonical dump must include every RMT mirror column.
    schema = await asyncio.to_thread(_read_schema_sql)
    memory_columns = _schema_columns(schema, "memory")
    missing = [c for c in persisted_columns() if c not in memory_columns]
    checks.append(
        OpsHealthCheck(
            name="memory_schema_contract",
            status="ok" if not missing else "fail",
            detail="loom.memory persisted columns match schema dump"
            if not missing
            else "schema dump is missing memory mirror columns",
            data={"missing_columns": missing},
            repair="run make schema-dump && make schema-check" if missing else "",
        )
    )

    missing_by_table: dict[str, list[str]] = {}
    for table, columns in REQUIRED_SCHEMA_COLUMNS.items():
        present = _schema_columns(schema, table)
        if not present:
            missing_by_table[table] = list(columns)
            continue
        missing_cols = [c for c in columns if c not in present]
        if missing_cols:
            missing_by_table[table] = missing_cols
    checks.append(
        OpsHealthCheck(
            name="schema_contracts",
            status="ok" if not missing_by_table else "fail",
            detail="high-risk tables/columns match schema dump"
            if not missing_by_table
            else "schema dump is missing high-risk tables or columns",
            data={"missing_by_table": missing_by_table},
            repair="run migrations, then make schema-dump && make schema-check"
            if missing_by_table
            else "",
        )
    )

    try:
        n_stubs = await ops_store.embedding_stub_count(input.org, input.namespace, ch=ch)
        checks.append(
            OpsHealthCheck(
                name="embedding_stubs",
                status="ok" if n_stubs == 0 else "warn",
                detail=f"{n_stubs} memory embedding stubs pending",
                data={"pending": n_stubs},
                repair="check embedding_worker status or run a backfill pass" if n_stubs else "",
            )
        )
    except Exception as e:  # noqa: BLE001 — a failing probe is a fail check; the others still run
        checks.append(
            OpsHealthCheck(
                name="embedding_stubs",
                status="fail",
                detail=str(e),
                repair="check loom.memory_embeddings schema and worker logs",
            )
        )

    checks.append(await _clickhouse_parts_check(ch))

    try:
        from server.insights.app import op_mix as _op_mix
        from server.settings import settings as _settings

        mix = await _op_mix.op_mix(
            ch,
            org=input.org,
            namespace=input.namespace,
            window_days=_settings.ops_op_mix_window_days,
        )
        status, detail, repair = _op_mix.assess(
            mix,
            reads_per_write_warn=_settings.ops_op_mix_reads_per_write_warn,
            update_share_warn=_settings.ops_op_mix_update_share_warn,
            min_writes=_settings.ops_op_mix_min_writes,
        )
        checks.append(
            OpsHealthCheck(
                name="op_mix", status=status, detail=detail, data=mix.as_data(), repair=repair
            )
        )
    except Exception as e:  # noqa: BLE001 — a failing probe is a fail check; the others still run
        checks.append(
            OpsHealthCheck(
                name="op_mix",
                status="fail",
                detail=str(e),
                repair="check loom.agent_events (operation, success, agent columns)",
            )
        )

    try:
        from server.memory.app import procedure_plane_health as _pp
        from server.settings import settings as _settings

        plane = await _pp.procedure_plane(
            ch,
            org=input.org,
            namespace=input.namespace,
            window_days=_settings.ops_procedure_plane_window_days,
        )
        status, detail, repair = _pp.assess(
            plane,
            min_packs=_settings.ops_procedure_plane_min_packs,
            retention_warn=_settings.ops_procedure_plane_retention_warn,
            min_pairs=_settings.ops_procedure_plane_min_pairs,
        )
        checks.append(
            OpsHealthCheck(
                name="procedure_plane",
                status=status,
                detail=detail,
                data=plane.as_data(),
                repair=repair,
            )
        )
    except Exception as e:  # noqa: BLE001 — a failing probe is a fail check; the others still run
        checks.append(
            OpsHealthCheck(
                name="procedure_plane",
                status="fail",
                detail=str(e),
                repair="check loom.procedure_runs and agent_events out_procedure_keys",
            )
        )

    worker_status = get_registry().snapshot()
    failed_workers = [
        name for name, row in worker_status.items() if row.get("consecutive_failures", 0)
    ]
    checks.append(
        OpsHealthCheck(
            name="workers",
            status="ok" if not failed_workers else "warn",
            detail="workers healthy" if not failed_workers else "workers have recent failures",
            data={"failed_workers": failed_workers},
            repair="inspect worker logs and rerun the failed worker pass" if failed_workers else "",
        )
    )

    if any(c.status == "fail" for c in checks):
        overall: Literal["ok", "warn", "fail"] = "fail"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"
    return OpsHealthOutput(checks=checks, worker_status=worker_status, overall=overall)


def _projection_failure_row(row: Any) -> ProjectionFailureRow:
    return ProjectionFailureRow(
        failure_id=row.failure_id,
        org=row.org,
        namespace=row.namespace,
        agent=row.agent,
        projection=row.projection,
        source_operation=row.source_operation,
        source_resource_id=row.source_resource_id,
        memory_keys=row.memory_keys,
        error_type=row.error_type,
        error_message=row.error_message,
        payload=row.payload,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else None,
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
    )


async def handle_projection_failures_list(
    input: ProjectionFailureListInput,
) -> ProjectionFailureListOutput:
    """List recorded projection failures: derived rows (embeddings, graph edges, summaries) a write
    could not produce."""
    from server.auth.context import resolve_org_scope
    from server.memory.app.projection_failures import list_projection_failures

    # The op skips the transport's namespace gate, so the caller's org scope decides
    # which namespaces it reads: a restricted one it cannot read stays out, admins too.
    scope = await resolve_org_scope(input.org)
    if not scope.accessible:
        return ProjectionFailureListOutput(rows=[], total=0)
    rows = await list_projection_failures(
        org=input.org,
        namespace=input.namespace,
        projection=input.projection,
        status=input.status,
        limit=input.limit,
        scope_where=scope.where,
        scope_params=scope.params,
    )
    out = [_projection_failure_row(row) for row in rows]
    return ProjectionFailureListOutput(rows=out, total=len(out))


async def handle_projection_failures_resolve(
    input: ProjectionFailureResolveInput,
) -> ProjectionFailureResolveOutput:
    """Mark projection failures resolved by id without retrying them."""
    from server.auth.context import resolve_org_scope
    from server.memory.app.projection_failures import mark_projection_failures_resolved

    scope = await resolve_org_scope(input.org)
    if not scope.accessible:
        return ProjectionFailureResolveOutput(resolved=0)
    resolved = await mark_projection_failures_resolved(
        org=input.org,
        failure_ids=input.failure_ids,
        scope_where=scope.where,
        scope_params=scope.params,
    )
    return ProjectionFailureResolveOutput(resolved=resolved)


async def handle_projection_failures_retry(
    input: ProjectionFailureRetryInput,
) -> ProjectionFailureRetryOutput:
    """Retry open projection failures for a namespace, optionally one projection kind."""
    from server.auth.context import resolve_org_scope
    from server.memory.app.projection_failures import retry_projection_failures

    scope = await resolve_org_scope(input.org)
    if not scope.accessible:
        return ProjectionFailureRetryOutput(results=[], retried=0, resolved=0, abandoned=0)
    results = await retry_projection_failures(
        org=input.org,
        namespace=input.namespace,
        projection=input.projection,
        limit=input.limit,
        scope_where=scope.where,
        scope_params=scope.params,
    )
    rows = [
        ProjectionFailureRetryRow(
            failure_id=r.failure_id,
            projection=r.projection,
            retried=r.retried,
            resolved=r.resolved,
            abandoned=r.abandoned,
            reason=r.reason,
            error=r.error,
        )
        for r in results
    ]
    return ProjectionFailureRetryOutput(
        results=rows,
        retried=sum(1 for r in rows if r.retried),
        resolved=sum(1 for r in rows if r.resolved),
        abandoned=sum(1 for r in rows if r.abandoned),
    )


async def handle_ops_capabilities(input: OpsCapabilitiesInput) -> OpsCapabilitiesOutput:
    from server.operations.capabilities import capability_snapshot

    snap = capability_snapshot()
    return OpsCapabilitiesOutput(
        build_commit=snap["build_commit"],
        schema_version=snap["schema_version"],
        ops=[OpCapability(name=name, **spec) for name, spec in snap["ops"].items()],
    )
