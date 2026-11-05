"""A resolved `knowledge.query` plan as one read-only statement over
loom.dataset_rows, and its run.

Nothing a caller wrote reaches the SQL: each dataset is a subquery named by
its place in the plan (`q0`, `q1`, ...), its columns are the slots its schema
names (SLOT_COLUMNS), its result columns are `c0`, `c1`, ..., and every value
is a typed parameter. Each subquery is held to its dataset's snapshot in the
caller's org and namespace, with its filters, before any join, so a row of
another tenant or snapshot is never joined, counted or summed.

A sum of a decimal column is taken in Decimal256, which no sum of the rows a
dataset holds overflows, and an average is that sum over the count, to nine
places (ClickHouse's `avg` of a decimal is a double).
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.datasets import SLOT_COLUMNS
from server.knowledge.domain.query import Column, Filter, Output, Resolved
from server.knowledge.store.datasets import ROWS

# The parameter type of each slot kind (schemas/migrations/083_knowledge_datasets.sql).
_PARAM = {
    "s": "String",
    "n": "Decimal128(9)",
    "f": "Float64",
    "t": "DateTime64(6, 'UTC')",
    "b": "Bool",
}
_COMPARE = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_BUCKET = {"day": "toDate({})", "month": "toStartOfMonth({})", "year": "toStartOfYear({})"}


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def add(self, value: Any, typ: str) -> str:
        """A placeholder of `value`, typed in the statement."""
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f"{{{name}:{typ}}}"


def _slot(c: Column) -> str:
    slot = c.column.slot
    if slot not in SLOT_COLUMNS:  # a schema the server wrote names only these
        raise ValueError(f"{slot!r} is not a slot")
    return slot


def _ref(c: Column) -> str:
    return f"q{c.index}.{_slot(c)}"


def _read(c: Column) -> str:
    """The column's value as its type reads: a date's slot holds a UTC
    date-time at midnight, which reads as the date."""
    return f"toDate({_ref(c)})" if c.column.type == "date" else _ref(c)


def _of(o: Output) -> str:
    """A group's or a selected column's value: each names a column."""
    assert o.column is not None  # groups and selected columns are built with one
    return _read(o.column)


def _condition(f: Filter, p: _Params) -> str:
    slot = _slot(f.column)
    typ = _PARAM[slot[0]]
    if f.op == "is_null":
        return f"{slot} IS NULL"
    if f.op == "not_null":
        return f"{slot} IS NOT NULL"
    if f.op in ("in", "not_in"):
        values = p.add(list(f.values), f"Array({typ})")
        return f"{slot} {'IN' if f.op == 'in' else 'NOT IN'} {values}"
    value = p.add(f.values[0], typ)
    if f.op == "contains":
        return f"position({slot}, {value}) > 0"
    if f.op == "starts_with":
        return f"startsWith({slot}, {value})"
    return f"{slot} {_COMPARE[f.op]} {value}"


def _aggregate(o: Output) -> str:
    if o.column is None:
        return "count()"
    x = _ref(o.column)
    decimal = o.column.column.slot[0] == "n"
    if o.fn == "count":
        return f"count({x})"
    if o.fn == "count_distinct":
        return f"uniqExact({x})"
    if o.fn == "sum":
        return f"sum(toDecimal256({x}, 9))" if decimal else f"sum({x})"
    if o.fn == "avg":
        # NULL for no values: a decimal divided by a NULL count is NULL, not an error.
        return f"sum(toDecimal256({x}, 9)) / nullIf(count({x}), 0)" if decimal else f"avg({x})"
    return f"{o.fn}({_read(o.column)})"  # min, max


def compile_plan(plan: Resolved, org: str, namespace: str) -> tuple[str, dict[str, Any]]:
    """The statement and its parameters."""
    p = _Params()
    scope = f"org = {p.add(org, 'String')} AND namespace = {p.add(namespace, 'String')}"
    used: list[set[str]] = [set() for _ in plan.catalogs]
    for f in plan.filters:
        used[f.column.index].add(_slot(f.column))
    for left, right in plan.joins:
        used[left.index].add(_slot(left))
        used[right.index].add(_slot(right))
    for o in plan.outputs:
        if o.column is not None:
            used[o.column.index].add(_slot(o.column))
    subqueries = []
    for i, catalog in enumerate(plan.catalogs):
        held = [
            f"dataset_id = {p.add(str(catalog['dataset_id']), 'String')}",
            f"snapshot_id = {p.add(int(catalog['snapshot_id']), 'UInt64')}",
            *(_condition(f, p) for f in plan.filters if f.column.index == i),
        ]
        cols = ", ".join(["row_id", *sorted(used[i])])
        subqueries.append(
            f"(SELECT {cols} FROM {ROWS} WHERE {scope} AND {' AND '.join(held)}) AS q{i}"
        )
    source = subqueries[0]
    for k, (left, right) in enumerate(plan.joins, start=1):
        source += f" INNER JOIN {subqueries[k]} ON {_ref(left)} = {_ref(right)}"

    exprs = [
        _BUCKET[o.bucket].format(_ref(o.column)) if o.bucket and o.column else _of(o)
        for o in plan.groups
    ]
    exprs += [_aggregate(o) for o in plan.aggregates]
    exprs += [_of(o) for o in plan.selected]
    picked = [f"{e} AS c{i}" for i, e in enumerate(exprs)]
    if plan.selected:  # each row's id in each dataset, after the result columns
        picked += [f"q{i}.row_id AS r{i}" for i in range(len(plan.catalogs))]
    sql = f"SELECT {', '.join(picked)} FROM {source}"
    if plan.groups:
        sql += " GROUP BY " + ", ".join(f"c{i}" for i in range(len(plan.groups)))
    # The caller's order, then groups in their order or rows in their datasets'
    # order, so rows that tie come back the same way each time a limit cuts them.
    order = [f"c{i}{' DESC' if desc else ''}" for i, desc in plan.order]
    ordered = {i for i, _ in plan.order}
    if plan.groups:
        order += [f"c{i}" for i in range(len(plan.groups)) if i not in ordered]
    elif plan.selected:
        order += [f"r{i}" for i in range(len(plan.catalogs))]
    if order:
        sql += " ORDER BY " + ", ".join(order)
    # One row past the limit says whether more matched.
    sql += f" LIMIT {p.add(plan.limit + 1, 'UInt32')}"
    return sql, p.values


async def run(
    sql: str, params: dict[str, Any], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    ch = ch or _default_client()
    return await ch.query_all(sql, params=params)
