"""A `knowledge.query` plan checked against the schemas of the datasets it
reads and resolved to their slots: every column a plan names is one a schema
holds, every value a filter compares with is read as that column's type, and
every join, group and aggregate is one its columns' types allow. What a plan
names never becomes SQL: the store compiles the resolved plan from slot names
and parameters alone.

Pure and I/O-free. A plan that does not hold raises ValueError with the reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from server.knowledge.domain import datasets as ds

# Which types each comparison and aggregate applies to; count and count_distinct take any.
_NUMERIC = frozenset({"number", "duration"})
_ORDERED = frozenset({"number", "duration", "date", "datetime", "string", "time"})
_ORDERED_OPS = frozenset({"lt", "le", "gt", "ge"})
_TEXT_OPS = frozenset({"contains", "starts_with"})


@dataclass(frozen=True)
class Column:
    """A column of one of the plan's datasets: `index` is the dataset's place in the plan."""

    index: int
    column: ds.DatasetColumn


@dataclass(frozen=True)
class Filter:
    column: Column
    op: str
    values: tuple[Any, ...]  # slot values: one for a comparison, none for is_null, any for in


@dataclass(frozen=True)
class Output:
    """A result column: a group (with its bucket), an aggregate or a selected column."""

    name: str
    type: str
    column: Column | None
    fn: str = ""  # an aggregate's
    bucket: str = ""  # a group's


@dataclass(frozen=True)
class Resolved:
    catalogs: tuple[Mapping[str, Any], ...]  # the datasets' catalog rows, in plan order
    filters: tuple[Filter, ...]
    joins: tuple[tuple[Column, Column], ...]  # the join of dataset 1, 2, ... in order
    groups: tuple[Output, ...]
    aggregates: tuple[Output, ...]
    selected: tuple[Output, ...]
    order: tuple[tuple[int, bool], ...]  # (result column, descending)
    limit: int

    @property
    def outputs(self) -> tuple[Output, ...]:
        return self.groups + self.aggregates + self.selected


def resolve(plan: Mapping[str, Any], catalogs: Mapping[str, Mapping[str, Any]]) -> Resolved:
    """`plan` (a `knowledge.query` input's fields) over `catalogs`, each
    dataset's catalog row by its alias."""
    aliases = [d["alias"] for d in plan["datasets"]]  # distinct: the input model checks
    schemas = [ds.columns_from_json(_schema(catalogs[a])) for a in aliases]

    def column(ref: Mapping[str, Any]) -> Column:
        alias = ref.get("dataset") or ""
        if not alias:
            if len(aliases) > 1:
                raise ValueError(
                    f"column {ref['column']!r}: name its dataset; the plan reads several"
                )
            alias = aliases[0]
        if alias not in aliases:
            raise ValueError(
                f"column {ref['column']!r}: no dataset in the plan is called {alias!r}"
            )
        i = aliases.index(alias)
        found = next((c for c in schemas[i] if c.name == ref["column"]), None)
        if found is None:
            raise ValueError(f"dataset {alias!r} has no column {ref['column']!r}")
        if not found.slot:
            raise ValueError(
                f"column {found.name!r} of {alias!r} is not stored: its type's slots are taken"
            )
        return Column(i, found)

    filters = tuple(_filter(column(f["column"]), f) for f in plan.get("where") or ())
    joins = _joins(plan.get("joins") or (), aliases, column)
    groups = tuple(_group(column(g["column"]), g) for g in plan.get("group_by") or ())
    aggregates = tuple(
        _aggregate(column(a["column"]) if a.get("column") else None, a)
        for a in plan.get("aggregates") or ()
    )
    if plan.get("select") and (aggregates or groups):
        raise ValueError("select returns rows; a plan with aggregates or groups cannot also select")
    if groups and not aggregates:
        raise ValueError("group_by needs an aggregate to calculate per group")
    if not aggregates and not plan.get("select"):
        raise ValueError("the plan needs aggregates, or columns to select")
    selected = tuple(_selected(column(s), aliases) for s in plan.get("select") or ())
    outputs = groups + aggregates + selected
    names = [o.name for o in outputs]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ValueError(f"result columns need names of their own: {', '.join(repeated)}")
    order = []
    for o in plan.get("order_by") or ():
        if o["by"] not in names:
            raise ValueError(f"order_by {o['by']!r} is not a result column")
        order.append((names.index(o["by"]), bool(o.get("desc"))))
    return Resolved(
        catalogs=tuple(catalogs[a] for a in aliases),
        filters=filters,
        joins=joins,
        groups=groups,
        aggregates=aggregates,
        selected=selected,
        order=tuple(order),
        limit=int(plan["limit"]),
    )


def _schema(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(json.loads(catalog["schema"]))


def _value(v: Any, col: ds.DatasetColumn, op: str) -> Any:
    """A filter's value as its column's slot holds it. A number a decimal slot
    would round is refused: compared rounded, `ge 0.0000000004` would match 0."""
    if v is None:
        raise ValueError(f"{op} on {col.name!r} needs a value; is_null finds nulls")
    if col.type == "duration" and isinstance(v, (int, float)) and not isinstance(v, bool):
        col = replace(col, type="number")  # seconds, as the slot holds them
    held = ds.slot_value(v, col)
    exact = _exact(v)
    if isinstance(held, Decimal) and exact is not None and held != exact:
        raise ValueError(
            f"{op} on {col.name!r}: {str(v)[:40]} has more than the nine decimal places"
            " a number column holds"
        )
    return held


def _exact(v: Any) -> Decimal | None:
    """`v` as the decimal it says, before any rounding; None for text that is no number
    (a duration's H:MM:SS)."""
    try:
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v).strip())
    except InvalidOperation:
        return None


def _filter(c: Column, f: Mapping[str, Any]) -> Filter:
    op, col = f["op"], c.column
    if op in ("is_null", "not_null"):
        return Filter(c, op, ())
    if op in _TEXT_OPS and col.type != "string":
        raise ValueError(f"{op} applies to text; {col.name!r} is {col.type}")
    if op in _ORDERED_OPS and col.type not in _ORDERED:
        raise ValueError(f"{op} applies to ordered values; {col.name!r} is {col.type}")
    if op in ("in", "not_in"):
        if not f.get("values"):
            raise ValueError(f"{op} on {col.name!r} needs values")
        return Filter(c, op, tuple(_value(v, col, op) for v in f["values"]))
    return Filter(c, op, (_value(f.get("value"), col, op),))


def _joins(
    raw: Any, aliases: list[str], column: Callable[[Mapping[str, Any]], Column]
) -> tuple[tuple[Column, Column], ...]:
    if len(raw) != len(aliases) - 1:
        raise ValueError(
            f"{len(aliases)} datasets need {len(aliases) - 1} joins, one per dataset past the first"
        )
    out = []
    for k, j in enumerate(raw, start=1):
        left, right = column(j["left"]), column(j["right"])
        if right.index != k:
            raise ValueError(
                f"join {k}: its right column must be of {aliases[k]!r}, the dataset it joins"
            )
        if left.index >= k:
            raise ValueError(
                f"join {k}: its left column must be of a dataset before {aliases[k]!r}"
            )
        if left.column.slot[0] != right.column.slot[0] or left.column.type != right.column.type:
            raise ValueError(
                f"join {k}: {left.column.name!r} ({left.column.type}) and"
                f" {right.column.name!r} ({right.column.type}) are not held alike,"
                " so they cannot be equal"
            )
        out.append((left, right))
    return tuple(out)


def _group(c: Column, g: Mapping[str, Any]) -> Output:
    bucket, col = g.get("bucket") or "", c.column
    if bucket and col.type not in ("date", "datetime"):
        raise ValueError(f"bucket {bucket} applies to dates; {col.name!r} is {col.type}")
    return Output(g.get("name") or col.name, "date" if bucket else col.type, c, bucket=bucket)


def _aggregate(c: Column | None, a: Mapping[str, Any]) -> Output:
    fn, name = a["fn"], a["name"]
    if c is None:
        if fn != "count":
            raise ValueError(f"{fn} needs a column")
        return Output(name, "number", None, fn=fn)
    t = c.column.type
    if fn in ("sum", "avg") and t not in _NUMERIC:
        raise ValueError(f"{fn} applies to numbers and durations; {c.column.name!r} is {t}")
    if fn in ("min", "max") and t not in _ORDERED:
        raise ValueError(f"{fn} applies to ordered values; {c.column.name!r} is {t}")
    return Output(name, "number" if fn.startswith("count") else t, c, fn=fn)


def _selected(c: Column, aliases: list[str]) -> Output:
    name = c.column.name if len(aliases) == 1 else f"{aliases[c.index]}.{c.column.name}"
    return Output(name, c.column.type, c)
