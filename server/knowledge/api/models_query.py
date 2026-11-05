"""Wire models of `knowledge.query`: a typed plan over published datasets, and
its typed result with the receipt that names the snapshots it read."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

# One filter value's text and one list's in all: each is one parameter of the
# statement, which ClickHouse takes up to 128 KiB of, escaping included.
MAX_VALUE_CHARS = 1024
MAX_LIST_CHARS = 60_000

# A value a filter compares with: read as the column's type (a date as ISO text).
Scalar = Annotated[str, Field(max_length=MAX_VALUE_CHARS)] | int | float | bool

# Bounds of one plan: what one request may ask, not a tunable.
MAX_DATASETS = 4
MAX_FILTERS = 32
MAX_GROUPS = 8
MAX_AGGREGATES = 16
MAX_SELECTED = 32
MAX_FILTER_VALUES = 1000
MAX_ROWS = 1000
MAX_ORDER = 4

_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"

FilterOp = Literal[
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "not_in",
    "is_null",
    "not_null",
    "contains",
    "starts_with",
]
AggregateFn = Literal["count", "count_distinct", "sum", "avg", "min", "max"]


class QueryDataset(BaseModel):
    # The name column references use for this dataset (default d); each dataset needs its own.
    alias: str = Field(default="d", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    # The ingested source the dataset belongs to.
    source_uri: str = Field(min_length=1)
    # The dataset, as knowledge.dataset_get names it; its live snapshot is read.
    dataset_id: str = Field(min_length=1)


class QueryColumn(BaseModel):
    # The alias of the dataset that holds the column; empty when the plan has one dataset.
    dataset: str = ""
    # The column's name, as knowledge.dataset_get lists it.
    column: str = Field(min_length=1, max_length=500)


class QueryFilter(BaseModel):
    # The column compared; every filter must hold for a row to count (AND).
    column: QueryColumn
    # The comparison: contains and starts_with (case-sensitive) apply to text; lt, le, gt
    # and ge to ordered types; a null meets none but is_null (ne and not_in skip it too).
    op: FilterOp
    # The value compared with, read as the column's type: a date or date-time as ISO text (an
    # offset read as UTC), a duration as seconds or H:MM:SS, a number to at most nine decimal
    # places (more is refused); in, not_in and the null tests take none.
    value: Scalar | None = None
    # The values of in and not_in: at most 1,000, of 60,000 characters in all.
    values: list[Scalar] = Field(default_factory=list, max_length=MAX_FILTER_VALUES)

    @model_validator(mode="after")
    def _values_fit_a_parameter(self) -> QueryFilter:
        if sum(len(str(v)) for v in self.values) > MAX_LIST_CHARS:
            raise ValueError(f"in and not_in values may be {MAX_LIST_CHARS} characters in all")
        return self


class QueryJoin(BaseModel):
    # A column of a dataset already in the plan.
    left: QueryColumn
    # The column of the dataset joined, equal to `left` on every joined row (an inner join).
    right: QueryColumn


class QueryGroup(BaseModel):
    # The column the rows are grouped by.
    column: QueryColumn
    # For a date or date-time column, the period it is grouped by (UTC); empty for its value.
    bucket: Literal["", "day", "month", "year"] = ""
    # The result column's name; empty for the column's own.
    name: str = Field(default="", pattern=r"^$|" + _NAME)


class QueryAggregate(BaseModel):
    # The calculation: count (rows, or a column's values), count_distinct, sum, avg, min or max.
    fn: AggregateFn
    # The column calculated over; omitted for count, which then counts rows.
    column: QueryColumn | None = None
    # The result column's name.
    name: str = Field(pattern=_NAME)


class QueryOrder(BaseModel):
    # A result column's name: a group's or an aggregate's, or a selected column's
    # (`<alias>.<column>` when the plan reads several datasets).
    by: str = Field(min_length=1)
    # True for the largest first.
    desc: bool = False


class KnowledgeQueryInput(BaseModel):
    """Calculate over the live snapshots of ingested datasets with a typed
    plan: filters, inner joins, groups and aggregates, or the rows themselves.
    Every row of the datasets is read, never a sample, and the receipt names
    the snapshots."""

    org: str
    namespace: str
    # The datasets the plan reads, each under its alias.
    datasets: list[QueryDataset] = Field(min_length=1, max_length=MAX_DATASETS)
    # The conditions every row read must meet, applied in each dataset before any join.
    where: list[QueryFilter] = Field(default_factory=list, max_length=MAX_FILTERS)
    # How the datasets past the first join one already in the plan: one join each.
    joins: list[QueryJoin] = Field(default_factory=list, max_length=MAX_DATASETS - 1)
    # The columns the aggregates are calculated per; empty for one result row.
    group_by: list[QueryGroup] = Field(default_factory=list, max_length=MAX_GROUPS)
    # The calculations; empty to return the rows themselves (`select`).
    aggregates: list[QueryAggregate] = Field(default_factory=list, max_length=MAX_AGGREGATES)
    # The columns of each row returned when the plan has no aggregates; each is named
    # `<alias>.<column>` in the result when the plan reads several datasets.
    select: list[QueryColumn] = Field(default_factory=list, max_length=MAX_SELECTED)
    # The result's order, by result column names; groups, or rows in source order, by default.
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=MAX_ORDER)
    # The result rows returned at most; `truncated` says when more matched.
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)

    @model_validator(mode="after")
    def _aliases_are_distinct(self) -> KnowledgeQueryInput:
        aliases = [d.alias for d in self.datasets]
        if len(set(aliases)) != len(aliases):
            raise ValueError("each dataset needs an alias of its own")
        return self


class QueryResultColumn(BaseModel):
    # The column's name: a selected column's, a group's or an aggregate's.
    name: str
    # Its type: string, number, bool, date, datetime, time or duration (seconds).
    type: str


class QuerySnapshot(BaseModel):
    # The dataset's alias in the plan.
    alias: str
    # The source it was published from.
    source_uri: str
    # The dataset read.
    dataset_id: str
    # The source revision the snapshot was published from.
    revision: int
    # The snapshot read: the rows the result was calculated over.
    snapshot_id: str
    # The rows the snapshot holds.
    row_count: int
    # The source's rows not in the snapshot, since a value did not fit its column's type.
    rejected_rows: int
    # False when the extraction left part of the table out.
    complete: bool
    # inferred or approved: who decided the columns' types.
    schema_state: str


class QueryReceipt(BaseModel):
    # The snapshots read, one per dataset of the plan.
    snapshots: list[QuerySnapshot]
    # The plan as validated, without org and namespace.
    plan: dict[str, Any]
    # The read-only statement the plan compiled to; every value in it is a parameter.
    sql: str
    # The statement's parameters, as text.
    params: dict[str, str]


class KnowledgeQueryOutput(BaseModel):
    # The result's columns, in order.
    columns: list[QueryResultColumn]
    # The result's rows; a decimal or an integer JSON cannot hold exactly is its text.
    rows: list[list[Any]]
    # With no aggregates, each returned row's row id in each dataset, in the plan's dataset order.
    row_ids: list[list[int]] = Field(default_factory=list)
    # True when more rows matched than `limit` returned.
    truncated: bool = False
    # False when a dataset read is incomplete or rejected rows: the result may not cover the source.
    complete: bool
    # Why the result is not complete, one reason per dataset.
    incomplete: list[str] = Field(default_factory=list)
    # What was read and how.
    receipt: QueryReceipt
