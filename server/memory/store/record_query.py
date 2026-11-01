"""The statements over a namespace's records: memory.record.query's field
predicates over the typed field rows (one row per record), and the count of its
active records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.record_values import FIELD_RE

# A record a read serves: its active version, not tombstoned.
_LIVE_RECORD = "{r}status = 'active' AND {r}tombstoned_at IS NULL"

_OP_SQL = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _build_record_query(input: Any) -> tuple[str, dict[str, Any]]:
    """Build (sql, params) for memory.record.query. Pure (no I/O) so the
    predicate logic is unit-testable. Every value is parameterized; field names
    are validated against FIELD_RE; ops and states come from fixed allowlists,
    so no caller input is interpolated into SQL.

    Predicates are AND-composed as one `sumIf(...) > 0` HAVING term each over a
    single record/fields JOIN. A `ne` predicate requires the field to be present
    with a concrete value that differs from the target (absent/unknown does not
    satisfy `ne`)."""
    where = [
        "r.org = {org:String}",
        "r.namespace = {ns:String}",
        "r.schema_name = {schema:String}",
        _LIVE_RECORD.format(r="r."),
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "schema": input.schema_name,
        "lim": input.limit + 1,
    }
    if input.as_of is not None:
        where.append(
            "(r.valid_at IS NULL OR r.valid_at <= {as_of:DateTime}) "
            "AND (r.valid_to IS NULL OR r.valid_to >= {as_of:DateTime})"
        )
        params["as_of"] = input.as_of

    having: list[str] = []

    def _add_positive(field_param: str, value_clause: str) -> None:
        having.append(
            f"sumIf(1, f.field_name = {{{field_param}:String}} "
            f"AND f.tombstoned_at IS NULL AND {value_clause}) > 0"
        )

    def _add_negative(field_param: str, ne_clause: str) -> None:
        # `ne` only matches a field present with a concrete value that differs
        # from the target — an absent/unknown field does NOT satisfy it.
        having.append(
            f"sumIf(1, f.field_name = {{{field_param}:String}} "
            f"AND f.tombstoned_at IS NULL AND f.value_state = 'value' "
            f"AND {ne_clause}) > 0"
        )

    i = 0
    # Exact-string filters: field == value, value_state='value'.
    for field_name, value in input.filters.items():
        if not FIELD_RE.match(field_name):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BAD_FIELD_NAME",
                    "message": f"invalid field {field_name!r}",
                },
            )
        fn, vp = f"fn{i}", f"v{i}"
        params[fn] = field_name
        params[vp] = str(value)
        _add_positive(fn, f"f.value_state = 'value' AND f.value_string = {{{vp}:String}}")
        i += 1

    # Typed predicates: numeric/bool/string comparisons + state/negative queries.
    for pred in input.predicates:
        if not FIELD_RE.match(pred.field):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BAD_FIELD_NAME",
                    "message": f"invalid field {pred.field!r}",
                },
            )
        op = _OP_SQL.get(pred.op, "=")
        fn = f"fn{i}"
        params[fn] = pred.field
        if pred.number is not None:
            vp = f"v{i}"
            params[vp] = float(pred.number)
            if pred.op == "ne":
                _add_negative(fn, f"f.value_number != {{{vp}:Float64}}")
            else:
                _add_positive(
                    fn,
                    f"f.value_state = 'value' AND f.value_number {op} {{{vp}:Float64}}",
                )
        elif pred.boolean is not None:
            vp = f"v{i}"
            params[vp] = bool(pred.boolean)
            if pred.op == "ne":
                _add_negative(fn, f"f.value_bool != {{{vp}:Bool}}")
            else:
                _add_positive(fn, f"f.value_state = 'value' AND f.value_bool {op} {{{vp}:Bool}}")
        elif pred.string is not None:
            vp = f"v{i}"
            params[vp] = str(pred.string)
            if pred.op == "ne":
                _add_negative(fn, f"f.value_string != {{{vp}:String}}")
            else:
                _add_positive(
                    fn,
                    f"f.value_state = 'value' AND f.value_string {op} {{{vp}:String}}",
                )
        elif pred.value_state is not None:
            # Safe to inline: value_state is constrained by the Literal type.
            _add_positive(fn, f"f.value_state = '{pred.value_state}'")
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EMPTY_PREDICATE",
                    "message": (f"predicate on {pred.field!r} needs a value or value_state"),
                },
            )
        i += 1

    if not having:
        # Zero predicates: plain record scan, no JOIN / GROUP BY needed.
        sql = (
            "SELECT toString(r.record_id) AS record_id "
            "FROM loom.memory_records AS r FINAL "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY r.updated_at DESC, r.record_id "
            "LIMIT {lim:UInt32}"
        )
        return sql, params

    sql = (
        "SELECT toString(r.record_id) AS record_id "
        "FROM loom.memory_records AS r FINAL "
        "LEFT JOIN loom.memory_record_fields AS f FINAL "
        "ON r.org = f.org AND r.namespace = f.namespace "
        "AND r.schema_name = f.schema_name AND r.record_id = f.record_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY r.record_id, r.updated_at "
        f"HAVING {' AND '.join(having)} "
        "ORDER BY r.updated_at DESC, r.record_id "
        "LIMIT {lim:UInt32}"
    )
    return sql, params


async def active_record_tally(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """Active records of the namespace (a rewritten record's earlier version is a
    `superseded` row), and the newest write among them."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace}
    row = await ch.query_one(
        "SELECT count() AS n, if(n = 0, NULL, max(updated_at)) AS latest "
        "FROM loom.memory_records FINAL WHERE org = {org:String} AND namespace = {ns:String} "
        f"AND {_LIVE_RECORD.format(r='')}",
        params,
    )
    return (int(row["n"]), row["latest"]) if row else (0, None)
