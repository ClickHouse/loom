"""Schema-grounded memory-record handlers — memory_schema.* + memory.record.*."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException

from server.core.ch import CHClient, caller_shaped, get_client
from server.core.datetimes import naive_utc_now
from server.memory.api.models_records import (
    MemoryRecordConflictRow,
    MemoryRecordConflictsInput,
    MemoryRecordConflictsOutput,
    MemoryRecordHistoryInput,
    MemoryRecordHistoryOutput,
    MemoryRecordQueryInput,
    MemoryRecordQueryOutput,
    MemoryRecordRow,
    MemoryRecordWriteInput,
    MemoryRecordWriteOutput,
    MemorySchemaDecisionInput,
    MemorySchemaDecisionOutput,
    MemorySchemaListInput,
    MemorySchemaListOutput,
    MemorySchemaRegisterInput,
    MemorySchemaRegisterOutput,
    MemorySchemaSummary,
    RecordFieldInput,
    SchemaFieldInput,
    SchemaFieldOutput,
)
from server.memory.domain import record_constraints
from server.memory.domain.decision_schema import SCHEMA_NAME
from server.memory.domain.record_values import FIELD_RE, _check_record_fields, coerce_record_value
from server.memory.store import active_record, next_version, supersede_active_record
from server.memory.store.record_query import _build_record_query, active_record_tally

_FIELD_TYPES = {"string", "number", "integer", "boolean"}


def _validate_schema_fields(fields: list[SchemaFieldInput]) -> None:
    seen: set[str] = set()
    for field in fields:
        if not FIELD_RE.match(field.name):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BAD_FIELD_NAME",
                    "message": f"invalid field name {field.name!r}",
                },
            )
        if field.name in seen:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DUPLICATE_FIELD",
                    "message": f"duplicate field {field.name!r}",
                },
            )
        seen.add(field.name)
        if field.enum_values and field.type != "string":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BAD_ENUM_FIELD",
                    "message": "enum_values are supported only for string fields",
                },
            )
        reason = record_constraints.validate_shape(
            field.name, field.type, record_constraints.from_field(field)
        )
        if reason:
            raise HTTPException(
                status_code=400,
                detail={"code": "BAD_CONSTRAINT", "message": reason, "field": field.name},
            )


async def _schema_by_name(org: str, namespace: str, name: str) -> dict[str, Any] | None:
    return await get_client().query_one(
        """
        SELECT toString(schema_id) AS schema_id, name, description, status
        FROM loom.memory_schemas FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND name = {name:String}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "name": name},
    )


async def _schema_fields(org: str, namespace: str, schema_id: str) -> list[dict[str, Any]]:
    return await get_client().query_all(
        """
        SELECT field_name, field_type, required, identity, enum_values, constraints,
               description
        FROM loom.memory_schema_fields FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND schema_id = {schema_id:UUID}
          AND tombstoned_at IS NULL
        ORDER BY field_name
        """,
        params={"org": org, "ns": namespace, "schema_id": schema_id},
    )


def _stamped_author(body_value: str) -> str:
    """Who wrote the schema: the writing principal as `writer_actor` stamps
    every memory row (`human:` / `process:` + subject). The body's value
    counts only with no principal and no pipeline stage to stamp."""
    from server.memory.app.provenance import writer_actor

    return writer_actor(fallback=body_value)


async def handle_memory_schema_register(
    input: MemorySchemaRegisterInput,
) -> MemorySchemaRegisterOutput:
    _validate_schema_fields(input.fields)
    ch = get_client()
    now = naive_utc_now()
    schema_id = str(uuid.uuid4())
    version = next_version()
    await ch.insert(
        "loom.memory_schemas",
        [
            {
                "schema_id": schema_id,
                "org": input.org,
                "namespace": input.namespace,
                "name": input.name,
                "description": input.description,
                "status": input.status,
                "version": version,
                "created_at": now,
                "created_by": _stamped_author(input.created_by),
                "tombstoned_at": None,
            }
        ],
    )
    await ch.insert(
        "loom.memory_schema_fields",
        [
            {
                "org": input.org,
                "namespace": input.namespace,
                "schema_id": schema_id,
                "field_name": field.name,
                "field_type": field.type,
                "required": field.required,
                "identity": field.identity,
                "enum_values": list(field.enum_values),
                "constraints": record_constraints.dumps(record_constraints.from_field(field)),
                "description": field.description,
                "version": version,
                "created_at": now,
                "tombstoned_at": None,
            }
            for field in input.fields
        ],
    )
    return MemorySchemaRegisterOutput(
        schema_id=schema_id,
        name=input.name,
        field_count=len(input.fields),
        status=input.status,
    )


async def handle_memory_schema_list(
    input: MemorySchemaListInput,
) -> MemorySchemaListOutput:
    """List a namespace's record schemas, active ones only unless `include_proposed`."""

    status_filter = "" if input.include_proposed else "AND status = 'active'"
    rows = await get_client().query_all(
        f"""
        SELECT toString(schema_id) AS schema_id, name, description, status
        FROM loom.memory_schemas FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL
          {status_filter}
        ORDER BY name
        LIMIT {{lim:UInt32}}
        """,
        params={"org": input.org, "ns": input.namespace, "lim": input.limit},
    )
    schemas: list[MemorySchemaSummary] = []
    for row in rows:
        fields = await _schema_fields(input.org, input.namespace, str(row["schema_id"]))
        schemas.append(
            MemorySchemaSummary(
                schema_id=str(row["schema_id"]),
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                status=str(row.get("status") or "active"),
                fields=[
                    SchemaFieldOutput(
                        name=str(f["field_name"]),
                        type=str(f["field_type"]),
                        required=bool(f.get("required")),
                        identity=bool(f.get("identity")),
                        enum_values=list(f.get("enum_values") or []),
                        constraints=record_constraints.loads(f.get("constraints")),
                        description=str(f.get("description") or ""),
                    )
                    for f in fields
                ],
            )
        )
    return MemorySchemaListOutput(schemas=schemas)


async def handle_memory_schema_propose(
    input: MemorySchemaRegisterInput,
) -> MemorySchemaRegisterOutput:
    """Register a record schema as `proposed`, for an admin to approve or reject.

    memory_schemas is keyed by (org, namespace, name) under ReplacingMergeTree(version),
    so a proposal under the name of an active schema would replace it: a writer's
    proposal would demote an admin-registered schema (record writes then answer
    SCHEMA_NOT_FOUND) and approving it would activate the writer's fields. Refused (409);
    an admin changes an active schema with memory_schema.register."""
    existing = await _schema_by_name(input.org, input.namespace, input.name)
    if existing is not None and str(existing.get("status") or "") == "active":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SCHEMA_ACTIVE",
                "message": (
                    f"record schema {input.name!r} is active in {input.namespace}; propose "
                    "under another name, or ask an admin to register the change"
                ),
            },
        )
    return await handle_memory_schema_register(input.model_copy(update={"status": "proposed"}))


async def handle_memory_schema_approve(
    input: MemorySchemaDecisionInput,
) -> MemorySchemaDecisionOutput:
    """Approve (make active) or reject a proposed record schema."""

    row = await _schema_by_name(input.org, input.namespace, input.name)
    if not row:
        return MemorySchemaDecisionOutput(name=input.name, status="", updated=False)
    status = "active" if input.decision == "approve" else "rejected"
    now = naive_utc_now()
    await get_client().insert(
        "loom.memory_schemas",
        [
            {
                "schema_id": row["schema_id"],
                "org": input.org,
                "namespace": input.namespace,
                "name": input.name,
                "description": str(row.get("description") or ""),
                "status": status,
                "version": next_version(),
                "created_at": now,
                "created_by": _stamped_author(input.reviewed_by),
                "tombstoned_at": None,
            }
        ],
    )
    return MemorySchemaDecisionOutput(name=input.name, status=status, updated=True)


@dataclass(frozen=True)
class _RecordFold:
    """What folding an incoming record onto the key's active record produced."""

    record_json: dict[str, Any]
    fold_out: dict[str, Any]
    superseded_id: str
    conflict_rows: list[dict[str, Any]]
    filled_fields: list[str]


def _record_key(
    input: MemoryRecordWriteInput, field_defs: list[dict[str, Any]]
) -> tuple[str, Literal["caller", "identity", "anonymous"]]:
    """The record's logical key and where it came from."""
    # Logical key: the caller's, else derived from the schema's identity fields. An identity
    # field without a concrete value is a rejection, never a repair: an invented key would
    # match nothing later.
    memory_key = input.memory_key
    key_source: Literal["caller", "identity", "anonymous"] = "caller" if memory_key else "anonymous"
    identity_names = [str(f["field_name"]) for f in field_defs if bool(f.get("identity"))]
    if not memory_key and identity_names:
        from server.memory.domain.record_identity import canonical_record_key, canonical_value

        # A value that canonicalises to "" (for a non-name field: punctuation, symbols or
        # marks alone) has nothing to key on.
        missing_identity = [
            n
            for n in identity_names
            if n not in input.fields
            or input.fields[n].state != "value"
            or not canonical_value(n, input.fields[n].value)
        ]
        if missing_identity:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "MISSING_IDENTITY_FIELD",
                    "message": (
                        f"identity fields need a concrete value for "
                        f"{input.schema_name!r}: {missing_identity}"
                    ),
                    "fields": missing_identity,
                },
            )
        memory_key = canonical_record_key(
            input.schema_name, {n: input.fields[n].value for n in identity_names}
        )
        key_source = "identity"
    return memory_key, key_source


async def _fold_onto_active_record(
    ch: CHClient,
    input: MemoryRecordWriteInput,
    memory_key: str,
    record_json: dict[str, Any],
    *,
    record_id: str,
    version: int,
    now: datetime,
) -> _RecordFold:
    """Field-level fold onto the active record for this key: an empty incoming field keeps
    the survivor's value, spelling variants are agreement, lists union, and a real
    disagreement is recorded with both values before the incoming one wins. A decision
    record keeps nothing of the survivor."""
    fold_out: dict[str, Any] = {}
    superseded_id = ""
    conflict_rows: list[dict[str, Any]] = []
    filled_fields: list[str] = []
    if memory_key:
        prior_row = await active_record(
            input.org, input.namespace, input.schema_name, memory_key, ch=ch
        )
        if prior_row is not None:
            from server.memory.domain.record_fold import fold, restate

            try:
                prior_json = json.loads(prior_row.record_json or "{}")
            except json.JSONDecodeError:
                prior_json = {}
            # A decision is one writer's statement, and the survivor may be another
            # writer's: keeping its fields would serve them past their writer's delete.
            combine = restate if input.schema_name == SCHEMA_NAME else fold
            result = combine(prior_json, record_json)
            record_json = result.merged
            filled_fields = list(result.filled_from_prior)
            fold_out = result.as_dict()
            superseded_id = prior_row.record_id
            prior_mid = prior_row.memory_id
            conflict_rows = [
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "schema_name": input.schema_name,
                    "memory_key": memory_key,
                    "field_name": str(c["field"]),
                    "prior_value": json.dumps(c["prior"], default=str),
                    "incoming_value": json.dumps(c["incoming"], default=str),
                    "prior_record_id": superseded_id,
                    "incoming_record_id": record_id,
                    "prior_memory_id": prior_mid,
                    "incoming_memory_id": input.memory_id,
                    "detected_at": now,
                    "resolved_at": None,
                    "version": version,
                    "tombstoned_at": None,
                }
                for c in result.changed
            ]
    return _RecordFold(record_json, fold_out, superseded_id, conflict_rows, filled_fields)


def _record_field_rows(
    input: MemoryRecordWriteInput,
    field_map: dict[str, dict[str, Any]],
    *,
    record_id: str,
    version: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """One typed memory_record_fields row per incoming field."""
    rows: list[dict[str, Any]] = []
    for field_name, field in input.fields.items():
        field_def = field_map[field_name]
        field_type = str(field_def.get("field_type") or "string")
        if field_type not in _FIELD_TYPES:
            field_type = "string"
        value_string, value_number, value_bool = coerce_record_value(
            field.value,
            field_type,
            field_name=field_name,
            enum_values=list(field_def.get("enum_values") or []),
            state=field.state,
            constraints=record_constraints.loads(field_def.get("constraints")),
        )
        rows.append(
            {
                "org": input.org,
                "namespace": input.namespace,
                "schema_name": input.schema_name,
                "record_id": record_id,
                "field_name": field_name,
                "field_type": field_type,
                "value_string": value_string,
                "value_number": value_number,
                "value_bool": value_bool,
                "value_state": field.state,
                "evidence_id": field.evidence_id,
                "version": version,
                "created_at": now,
                "tombstoned_at": None,
            }
        )
    return rows


async def _carry_folded_fields(
    input: MemoryRecordWriteInput,
    rows: list[dict[str, Any]],
    fold: _RecordFold,
    *,
    record_id: str,
    version: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """`rows` plus the field rows the fold kept from the superseded record."""
    filled_fields, superseded_id = fold.filled_fields, fold.superseded_id
    # Fields the fold kept from the predecessor need field rows under the
    # new record_id too, or a predicate on them would miss this record.
    if filled_fields and superseded_id:
        carried = await get_client().query_all(
            """
            SELECT field_name, field_type, value_string, value_number, value_bool,
                   value_state, evidence_id
            FROM loom.memory_record_fields FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND record_id = {rid:UUID} AND field_name IN {names:Array(String)}
              AND tombstoned_at IS NULL
            """,
            {"org": input.org, "ns": input.namespace, "rid": superseded_id, "names": filled_fields},
        )
        present = {str(r["field_name"]) for r in rows}
        for c in carried:
            if str(c["field_name"]) in present:
                # the incoming row exists but is empty: replace it with the kept value
                rows = [r for r in rows if r["field_name"] != str(c["field_name"])]
            rows.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "schema_name": input.schema_name,
                    "record_id": record_id,
                    "field_name": str(c["field_name"]),
                    "field_type": str(c["field_type"]),
                    "value_string": c.get("value_string"),
                    "value_number": c.get("value_number"),
                    "value_bool": c.get("value_bool"),
                    "value_state": str(c.get("value_state") or "value"),
                    "evidence_id": c.get("evidence_id"),
                    "version": version,
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
    return rows


async def handle_memory_record_write(
    input: MemoryRecordWriteInput,
) -> MemoryRecordWriteOutput:
    schema = await _schema_by_name(input.org, input.namespace, input.schema_name)
    if not schema or str(schema.get("status")) != "active":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SCHEMA_NOT_FOUND",
                "message": f"schema {input.schema_name!r} not active",
            },
        )
    schema_id = str(schema["schema_id"])
    field_defs = await _schema_fields(input.org, input.namespace, schema_id)
    field_map = {str(f["field_name"]): f for f in field_defs}

    _check_record_fields(input, field_defs, field_map)

    memory_key, key_source = _record_key(input, field_defs)

    ch = get_client()
    record_id = str(uuid.uuid4())
    now = naive_utc_now()
    version = next_version()

    record_json = {
        name: (field.value if isinstance(field, RecordFieldInput) else field)
        for name, field in input.fields.items()
    }
    fold = await _fold_onto_active_record(
        ch, input, memory_key, record_json, record_id=record_id, version=version, now=now
    )
    record_json = fold.record_json
    # Supersede prior active state for this logical key before inserting: every
    # write mints a fresh record_id, so without this two writes for the same
    # memory_key both stay 'active'. Close the prior validity interval
    # (valid_to := new fact's valid_at, or now() when world-time is unknown) so
    # as-of reads still see it.
    if memory_key:
        await supersede_active_record(
            input.org,
            input.namespace,
            input.schema_name,
            memory_key,
            close_at=input.valid_at or now,
            version=version,
            now=now,
            ch=ch,
        )

    await get_client().insert(
        "loom.memory_records",
        [
            {
                "record_id": record_id,
                "org": input.org,
                "namespace": input.namespace,
                "schema_id": schema_id,
                "schema_name": input.schema_name,
                "memory_key": memory_key,
                "memory_id": input.memory_id,
                "identity_agent": input.agent,
                "user_id": input.user_id,
                "session_id": input.session_id,
                "record_json": json.dumps(record_json, sort_keys=True, default=str),
                "status": "active",
                "valid_at": input.valid_at,
                "valid_to": input.valid_to,
                "version": version,
                "created_at": now,
                "updated_at": now,
                "tombstoned_at": None,
            }
        ],
    )
    rows = _record_field_rows(input, field_map, record_id=record_id, version=version, now=now)
    rows = await _carry_folded_fields(
        input, rows, fold, record_id=record_id, version=version, now=now
    )
    await get_client().insert("loom.memory_record_fields", rows)
    if fold.conflict_rows:
        await get_client().insert("loom.memory_record_conflicts", fold.conflict_rows)
    return MemoryRecordWriteOutput(
        record_id=record_id,
        field_count=len(rows),
        memory_key=memory_key,
        key_source=key_source,
        fold=fold.fold_out,
        superseded_record_id=fold.superseded_id,
        conflicts=len(fold.conflict_rows),
    )


async def _records_by_ids(
    org: str,
    namespace: str,
    record_ids: list[str],
    *,
    history: bool = False,
) -> list[MemoryRecordRow]:
    if not record_ids:
        return []
    final = "" if history else "FINAL"
    # History reads the append-only mirror: loom.memory_records is a
    # ReplacingMergeTree(version), so once a merge runs it keeps only a
    # record's superseded row and the version that was active is gone. The
    # mirror can hold an exact replay of one version (its seed overlapping a
    # write), which LIMIT 1 BY folds.
    if history:
        source = "loom.memory_record_versions"
        order = "ORDER BY updated_at DESC, version DESC LIMIT 1 BY record_id, version"
        # Serves the tombstoned version a decision delete wrote; hides any other row with
        # tombstoned_at set.
        visible = "AND (status = 'tombstoned' OR tombstoned_at IS NULL)"
    else:
        source, order = "loom.memory_records FINAL", "ORDER BY updated_at DESC"
        visible = "AND tombstoned_at IS NULL"
    # `AS rid`/`AS mid`, never `AS record_id`/`AS memory_id`: a same-name
    # alias SHADOWS the column, so `record_id IN {ids:Array(UUID)}` would
    # compare the String alias against UUIDs and match nothing, so every hydration would
    # return [] (see server/code/store/index/edges.py live_edges_for_path).
    rec_rows = await get_client().query_all(
        f"""
        SELECT toString(record_id) AS rid, memory_key,
               toString(memory_id) AS mid, record_json, status, valid_at, valid_to
        FROM {source}
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND record_id IN {{ids:Array(UUID)}}
          {visible}
        {order}
        """,
        params={"org": org, "ns": namespace, "ids": record_ids},
    )
    field_rows = await get_client().query_all(
        f"""
        SELECT toString(record_id) AS rid, field_name, value_state
        FROM loom.memory_record_fields {final}
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND record_id IN {{ids:Array(UUID)}}
          AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "ids": record_ids},
    )
    states: dict[str, dict[str, str]] = {}
    for row in field_rows:
        rec_id = str(row["rid"])
        states.setdefault(rec_id, {})[str(row["field_name"])] = str(row["value_state"])
    out: list[MemoryRecordRow] = []
    for row in rec_rows:
        try:
            record = json.loads(str(row.get("record_json") or "{}"))
        except json.JSONDecodeError:
            record = {}
        rid = str(row["rid"])
        out.append(
            MemoryRecordRow(
                record_id=rid,
                memory_key=str(row.get("memory_key") or ""),
                memory_id=str(row.get("mid") or ""),
                record=record,
                field_states=states.get(rid, {}),
                status=row["status"],
                valid_at=row.get("valid_at"),
                valid_to=row.get("valid_to"),
            )
        )
    return out


async def handle_memory_record_query(
    input: MemoryRecordQueryInput,
) -> MemoryRecordQueryOutput:
    sql, params = _build_record_query(input)
    # the caller's filters shape the statement: the caller-shaped ceilings apply
    with caller_shaped():
        rows = await get_client().query_all(sql, params=params)
    ids = [str(r["record_id"]) for r in rows[: input.limit]]
    return MemoryRecordQueryOutput(
        rows=await _records_by_ids(input.org, input.namespace, ids),
        truncated=len(rows) > input.limit,
    )


async def handle_memory_record_history(
    input: MemoryRecordHistoryInput,
) -> MemoryRecordHistoryOutput:
    return MemoryRecordHistoryOutput(
        rows=await _records_by_ids(
            input.org,
            input.namespace,
            [input.record_id],
            history=True,
        )
    )


async def handle_memory_record_conflicts(
    input: MemoryRecordConflictsInput,
) -> MemoryRecordConflictsOutput:
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"org": input.org, "ns": input.namespace, "limit": input.limit + 1}
    if input.schema_name:
        where.append("schema_name = {schema:String}")
        params["schema"] = input.schema_name
    if input.memory_key:
        where.append("memory_key = {key:String}")
        params["key"] = input.memory_key
    if input.open_only:
        where.append("resolved_at IS NULL")
    rows = await get_client().query_all(
        f"""
        SELECT schema_name, memory_key, field_name, prior_value, incoming_value,
               toString(prior_record_id) AS prid, toString(incoming_record_id) AS irid,
               toString(prior_memory_id) AS pmid, toString(incoming_memory_id) AS imid,
               detected_at, resolved_at
        FROM loom.memory_record_conflicts FINAL
        WHERE {" AND ".join(where)}
        ORDER BY detected_at DESC, schema_name, memory_key, field_name
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )

    def _val(raw: Any) -> Any:
        try:
            return json.loads(str(raw)) if raw not in (None, "") else None
        except json.JSONDecodeError:
            return str(raw)

    out = [
        MemoryRecordConflictRow(
            schema_name=str(r["schema_name"]),
            memory_key=str(r["memory_key"]),
            field_name=str(r["field_name"]),
            prior_value=_val(r.get("prior_value")),
            incoming_value=_val(r.get("incoming_value")),
            prior_record_id=str(r["prid"]),
            incoming_record_id=str(r["irid"]),
            prior_memory_id=str(r.get("pmid") or ""),
            incoming_memory_id=str(r.get("imid") or ""),
            detected_at=r.get("detected_at"),
            resolved_at=r.get("resolved_at"),
        )
        for r in rows[: input.limit]
    ]
    return MemoryRecordConflictsOutput(rows=out, truncated=len(rows) > input.limit)


async def record_tally(org: str, namespace: str) -> tuple[int, datetime | None]:
    """How many active records the namespace holds, and the newest write."""
    return await active_record_tally(org, namespace)
