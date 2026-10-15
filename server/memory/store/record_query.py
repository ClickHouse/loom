"""The statement memory.record.query reads a schema's records with: field
predicates over the typed field rows, one row per record."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.memory.domain.record_values import _FIELD_RE
from server.memory.domain.temporal_sql import record_validity_predicate

_OP_SQL = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _record_predicates(input: Any, params: dict[str, Any]) -> list[str]:
    """The query's filters and predicates as `sumIf(...) > 0` HAVING terms over the
    record's field rows (alias `f`), their values added to `params`. The live and
    the historical query share them, so a revision matches as the record would."""
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
        if not _FIELD_RE.match(field_name):
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
        if not _FIELD_RE.match(pred.field):
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

    return having


def _build_record_query(input: Any) -> tuple[str, dict[str, Any]]:
    """Build (sql, params) for memory.record.query. Pure (no I/O) so the
    predicate logic is unit-testable. Every value is parameterized; field names
    are validated against _FIELD_RE; ops and states come from fixed allowlists,
    so no caller input is interpolated into SQL.

    Predicates are AND-composed as one `sumIf(...) > 0` HAVING term each over a
    single record/fields JOIN. A `ne` predicate requires the field to be present
    with a concrete value that differs from the target (absent/unknown does not
    satisfy `ne`)."""
    where = [
        "r.org = {org:String}",
        "r.namespace = {ns:String}",
        "r.schema_name = {schema:String}",
        "r.status = 'active'",
        "r.tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "schema": input.schema_name,
        "lim": input.limit + 1,
    }
    if input.valid_as_of is not None:
        where.append(record_validity_predicate("r"))
        params["valid_as_of"] = input.valid_as_of

    having = _record_predicates(input, params)

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


# Record revisions a query at K reads; past it the answer says it is truncated.
REVISIONS_LIMIT = 10_000


async def record_revisions_at(
    org: str,
    namespace: str,
    schema_name: str,
    known_ns: int,
    valid_at: datetime,
    *,
    erased_ns_us: int = 0,
    erased_users: Sequence[tuple[str, int]] = (),
    limit: int = REVISIONS_LIMIT,
) -> list[dict[str, Any]]:
    """Each record's newest revision written at or before K (`version` is the
    write's epoch ns), from the append-only mirror, kept when it is active and
    valid at V (`valid_at`, naive UTC): the limit counts only those. A revision
    written before an erasure of its namespace or user is never served."""
    from server.core.ch import caller_shaped, get_client

    with caller_shaped():
        return await get_client().query_all(
            """
            SELECT * FROM (
                SELECT record_id, memory_key, memory_id, record_json, status, valid_at,
                       valid_to, tombstoned_at, updated_at
                FROM loom.memory_record_versions
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND schema_name = {schema:String} AND version <= {k:UInt64}
                  AND version >= {er:UInt64}
                  AND NOT arrayExists(x -> x.1 = user_id AND version < x.2 * 1000,
                                      {er_users:Array(Tuple(String, Int64))})
                ORDER BY version DESC LIMIT 1 BY record_id
            )
            WHERE status = 'active' AND tombstoned_at IS NULL
              AND (valid_at IS NULL OR valid_at <= {v:DateTime})
              AND (valid_to IS NULL OR valid_to > {v:DateTime})
            LIMIT {lim:UInt32}
            """,
            params={
                "org": org,
                "ns": namespace,
                "schema": schema_name,
                "k": known_ns,
                "v": valid_at,
                "er": erased_ns_us * 1000,
                "er_users": list(erased_users),
                "lim": limit,
            },
        )


async def revisions_matching(input: Any, record_ids: Sequence[str]) -> set[str]:
    """Of these revisions, the ones whose own field rows (written under the
    revision's record_id, never changed after) meet the query's filters and
    predicates, with the live query's terms."""
    if not record_ids:
        return set()
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "schema": input.schema_name,
        "ids": list(record_ids),
    }
    having = _record_predicates(input, params)
    if not having:
        return set(record_ids)
    from server.core.ch import caller_shaped, get_client

    with caller_shaped():
        rows = await get_client().query_all(
            "SELECT toString(f.record_id) AS rid FROM loom.memory_record_fields AS f FINAL "
            "WHERE f.org = {org:String} AND f.namespace = {ns:String} "
            "AND f.schema_name = {schema:String} AND f.record_id IN {ids:Array(UUID)} "
            f"GROUP BY f.record_id HAVING {' AND '.join(having)}",
            params=params,
        )
    return {str(r["rid"]) for r in rows}
