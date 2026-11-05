"""knowledge.query: a plan checked against its datasets' schemas, the read-only
statement it compiles to, and the handler over faked stores (the live
snapshot pinned, a schema that needs an answer refused, a snapshot replaced
while the statement ran never released). The reference queries against a
real ClickHouse are in tests/integration/test_knowledge_query_live.py."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from server.core.ch import _raise_if_caller_cap, sequential_reads_var
from server.core.errors import NotFoundError, PreconditionFailedError, ValidationError
from server.knowledge.api.models_query import KnowledgeQueryInput
from server.knowledge.app import query as app
from server.knowledge.domain import query as plan_domain
from server.knowledge.store import dataset_query

ORDERS = [
    {"name": "team", "type": "string", "inferred": "string", "slot": "s1"},
    {"name": "amount", "type": "number", "inferred": "number", "slot": "n1"},
    {"name": "placed_at", "type": "datetime", "inferred": "datetime", "slot": "t1"},
    {"name": "late", "type": "duration", "inferred": "duration", "slot": "n2"},
    {"name": "score", "type": "number", "inferred": "number", "slot": "f1"},
    {"name": "spare", "type": "number", "inferred": "number", "slot": ""},
    {"name": "due", "type": "date", "inferred": "date", "slot": "t2"},
]
TEAMS = [
    {"name": "team", "type": "string", "inferred": "string", "slot": "s1"},
    {"name": "budget", "type": "number", "inferred": "number", "slot": "n1"},
]


def _catalog(dataset_id: str, schema: list[dict[str, str]], **kw: Any) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "source_uri": f"repo://{dataset_id}.csv",
        "dataset_id": dataset_id,
        "doc_version": 7,
        "snapshot_id": 41 if dataset_id == "orders" else 42,
        "revision": 2,
        "schema": json.dumps(schema),
        "schema_state": "inferred",
        "clarifications": "[]",
        "row_count": 5,
        "rejected_rows": 0,
        "complete": True,
        **kw,
    }


CATALOGS = {"o": _catalog("orders", ORDERS), "t": _catalog("teams", TEAMS)}


def _plan(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "datasets": [{"alias": "o", "source_uri": "repo://orders.csv", "dataset_id": "orders"}],
        "aggregates": [{"fn": "count", "name": "n"}],
    }
    return KnowledgeQueryInput(**{**base, **kw}).model_dump(exclude={"org", "namespace"})


def _col(column: str, dataset: str = "") -> dict[str, str]:
    return {"dataset": dataset, "column": column}


JOINED = [
    {"alias": "o", "source_uri": "repo://orders.csv", "dataset_id": "orders"},
    {"alias": "t", "source_uri": "repo://teams.csv", "dataset_id": "teams"},
]


# ---- the plan, checked against the schemas -------------------------------------


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (dict(aggregates=[{"fn": "sum", "column": _col("nope"), "name": "x"}]),
         "dataset 'o' has no column 'nope'"),
        (dict(aggregates=[{"fn": "sum", "column": _col("spare"), "name": "x"}]),
         "column 'spare' of 'o' is not stored"),
        (dict(aggregates=[{"fn": "sum", "column": _col("team"), "name": "x"}]),
         "sum applies to numbers and durations; 'team' is string"),
        (dict(aggregates=[{"fn": "avg", "name": "x"}]), "avg needs a column"),
        (dict(where=[{"column": _col("amount"), "op": "contains", "value": "1"}]),
         "contains applies to text; 'amount' is number"),
        (dict(where=[{"column": _col("amount"), "op": "eq", "value": "lots"}]),
         "'lots' is not a number"),
        (dict(where=[{"column": _col("amount"), "op": "gt"}]), "gt on 'amount' needs a value"),
        # Rounded to nine places, ge 0.0000000004 would match a row holding 0.
        (dict(where=[{"column": _col("amount"), "op": "ge", "value": "0.0000000004"}]),
         "0.0000000004 has more than the nine decimal places a number holds"),
        (dict(where=[{"column": _col("amount"), "op": "ge", "value": 4e-10}]),
         "4e-10 has more than the nine decimal places"),
        (dict(where=[{"column": _col("late"), "op": "ge", "value": "0:00:00.0000000004"}]),
         "0:00:00.0000000004 has more than the nine decimal places"),
        (dict(where=[{"column": _col("team"), "op": "in"}]), "in on 'team' needs values"),
        (dict(where=[{"column": _col("placed_at"), "op": "ge", "value": "soon"}]),
         "'soon' is not a datetime"),
        (dict(group_by=[{"column": _col("team"), "bucket": "month"}]),
         "bucket month applies to dates; 'team' is string"),
        (dict(group_by=[{"column": _col("team")}], aggregates=[]),
         "group_by needs an aggregate"),
        (dict(aggregates=[]), "the plan needs aggregates, or columns to select"),
        (dict(select=[_col("team")]), "a plan with aggregates or groups cannot also select"),
        (dict(group_by=[{"column": _col("team"), "name": "n"}]),
         "result columns need names of their own: n"),
        (dict(order_by=[{"by": "nope"}]), "order_by 'nope' is not a result column"),
        (dict(aggregates=[{"fn": "sum", "column": _col("amount", "x"), "name": "s"}]),
         "no dataset in the plan is called 'x'"),
        (dict(datasets=JOINED), "2 datasets need 1 joins"),
        (dict(datasets=JOINED, joins=[{"left": _col("team", "o"), "right": _col("team", "t")}],
              aggregates=[{"fn": "sum", "column": _col("amount"), "name": "s"}]),
         "column 'amount': name its dataset; the plan reads several"),
        (dict(datasets=JOINED, joins=[{"left": _col("team", "t"), "right": _col("team", "o")}]),
         "join 1: its right column must be of 't'"),
        (dict(datasets=JOINED, joins=[{"left": _col("score", "o"), "right": _col("budget", "t")}]),
         "'score' (number) and 'budget' (number) are not held alike"),
    ],
)  # fmt: skip
def test_a_plan_its_schemas_do_not_allow_is_refused_with_why(
    plan: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError) as e:
        plan_domain.resolve(_plan(**plan), CATALOGS)
    assert reason in str(e.value)


def test_a_repeated_alias_is_refused_before_any_dataset_is_looked_up() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="each dataset needs an alias of its own"):
        _plan(datasets=[JOINED[0], {**JOINED[1], "alias": "o"}])


def test_filter_values_are_read_as_their_columns_types() -> None:
    plan = plan_domain.resolve(
        _plan(where=[
            {"column": _col("amount"), "op": "ge", "value": "0.1"},
            {"column": _col("placed_at"), "op": "lt", "value": "2026-02-01T00:00:00+01:00"},
            {"column": _col("late"), "op": "gt", "value": 90},
            {"column": _col("late"), "op": "le", "value": "1:00:00"},
            {"column": _col("team"), "op": "in", "values": ["a", 7]},
        ]),
        CATALOGS,
    )  # fmt: skip
    values = [f.values for f in plan.filters]
    assert values[0] == (Decimal("0.100000000"),)
    assert str(values[1][0]) == "2026-01-31 23:00:00"  # an offset is read as UTC
    assert values[2] == (Decimal("90.000000000"),)  # a duration in seconds, or as H:MM:SS
    assert values[3] == (Decimal("3600.000000000"),)
    assert values[4] == ("a", "7")


# ---- the statement --------------------------------------------------------------


def test_the_statement_holds_no_caller_text_and_reads_each_dataset_in_its_scope() -> None:
    plan = plan_domain.resolve(
        _plan(
            datasets=JOINED,
            joins=[{"left": _col("team", "o"), "right": _col("team", "t")}],
            where=[{"column": _col("team", "t"), "op": "eq", "value": "x') OR 1=1 --"}],
            group_by=[{"column": _col("placed_at", "o"), "bucket": "month", "name": "month"}],
            aggregates=[
                {"fn": "sum", "column": _col("amount", "o"), "name": "total"},
                {"fn": "avg", "column": _col("amount", "o"), "name": "mean"},
            ],
            order_by=[{"by": "total", "desc": True}],
            limit=10,
        ),
        CATALOGS,
    )
    sql, params = dataset_query.compile_plan(plan, "o", "ns")
    assert sql == (
        "SELECT toStartOfMonth(q0.t1) AS c0, sum(toDecimal256(q0.n1, 9)) AS c1,"
        " sum(toDecimal256(q0.n1, 9)) / nullIf(count(q0.n1), 0) AS c2"
        " FROM (SELECT row_id, n1, s1, t1 FROM loom.dataset_rows WHERE org = {p0:String}"
        " AND namespace = {p1:String} AND dataset_id = {p2:String} AND snapshot_id = {p3:UInt64})"
        " AS q0 INNER JOIN (SELECT row_id, s1 FROM loom.dataset_rows WHERE org = {p0:String}"
        " AND namespace = {p1:String} AND dataset_id = {p4:String} AND snapshot_id = {p5:UInt64}"
        " AND s1 = {p6:String}) AS q1 ON q0.s1 = q1.s1"
        # The caller's order, then the groups', so ties come back the same way each time.
        " GROUP BY c0 ORDER BY c1 DESC, c0 LIMIT {p7:UInt32}"
        # A month as a Date32: Date ends at 1970 and 2149, a slot at 1900 and 2299.
        " SETTINGS enable_extended_results_for_datetime_functions = 1"
    )
    assert params == {
        "p0": "o", "p1": "ns", "p2": "orders", "p3": 41, "p4": "teams", "p5": 42,
        "p6": "x') OR 1=1 --", "p7": 11,
    }  # fmt: skip


def test_lists_and_null_tests_compile_to_typed_arrays_and_bare_slots() -> None:
    plan = plan_domain.resolve(
        _plan(where=[
            {"column": _col("amount"), "op": "in", "values": ["0.1", 2]},
            {"column": _col("team"), "op": "not_in", "values": ["a"]},
            {"column": _col("placed_at"), "op": "is_null"},
            {"column": _col("score"), "op": "not_null"},
        ]),
        CATALOGS,
    )  # fmt: skip
    sql, params = dataset_query.compile_plan(plan, "o", "ns")
    assert (
        " AND n1 IN {p4:Array(Decimal128(9))} AND s1 NOT IN {p5:Array(String)}"
        " AND t1 IS NULL AND f1 IS NOT NULL)"
    ) in sql
    assert params["p4"] == [Decimal("0.100000000"), Decimal("2.000000000")]


def test_rows_are_selected_with_their_row_ids_in_source_order() -> None:
    plan = plan_domain.resolve(
        _plan(aggregates=[], select=[_col("team"), _col("score")],
              where=[{"column": _col("score"), "op": "not_null"}]),
        CATALOGS,
    )  # fmt: skip
    sql, _ = dataset_query.compile_plan(plan, "o", "ns")
    assert sql.startswith("SELECT q0.s1 AS c0, q0.f1 AS c1, q0.row_id AS r0 FROM")
    assert "AND f1 IS NOT NULL)" in sql and sql.endswith("ORDER BY r0 LIMIT {p4:UInt32}")


def test_a_date_reads_as_a_date_wherever_it_is_returned() -> None:
    plan = plan_domain.resolve(
        _plan(group_by=[{"column": _col("due")}],
              aggregates=[{"fn": "max", "column": _col("due"), "name": "last"},
                          {"fn": "max", "column": _col("placed_at"), "name": "at"}]),
        CATALOGS,
    )  # fmt: skip
    sql, _ = dataset_query.compile_plan(plan, "o", "ns")
    # Its slot holds a UTC date-time at midnight; a date-time column stays one. A Date32,
    # since a Date wraps a date before 1970 or past 2149 to another.
    assert sql.startswith(
        "SELECT toDate32(q0.t2) AS c0, max(toDate32(q0.t2)) AS c1, max(q0.t1) AS c2"
    )


def test_a_schema_naming_a_column_no_slot_holds_is_not_compiled() -> None:
    bad = {"o": _catalog("orders", [{**ORDERS[0], "slot": "s1; DROP"}])}
    plan = plan_domain.resolve(
        _plan(aggregates=[{"fn": "count", "column": _col("team"), "name": "n"}]), bad
    )
    with pytest.raises(ValueError, match="is not a slot"):
        dataset_query.compile_plan(plan, "o", "ns")


# ---- the handler ----------------------------------------------------------------


class Stores:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.versions = {"repo://orders.csv": 7, "repo://teams.csv": 7}
        self.catalogs = {"orders": CATALOGS["o"], "teams": CATALOGS["t"]}
        self.rows: list[dict[str, Any]] = [{"c0": 5}]
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.after_run: dict[str, int | None] = {}
        self.snapshot_after_run: dict[str, int] = {}
        self.shaped: list[bool] = []
        self.own_reads: list[bool] = []  # each lookup's and the scan's read-your-writes
        self.served = {7}  # the document versions whose catalog rows serve the datasets
        self.raises: BaseException | None = None
        monkeypatch.setattr(app.documents_repo, "live_versions", self.live_versions)
        monkeypatch.setattr(app.datasets_repo, "at_version", self.at_version)
        monkeypatch.setattr(app.dataset_query, "run", self.run)

    async def live_versions(self, org: str, namespace: str, uris: list[str]) -> dict[str, int]:
        self.own_reads.append(sequential_reads_var.get())
        return {u: v for u in uris if (v := self.versions.get(u)) is not None}

    async def at_version(
        self, org: str, ns: str, uri: str, v: int, *, dataset_id: str
    ) -> list[dict[str, Any]]:
        self.own_reads.append(sequential_reads_var.get())
        c = self.catalogs.get(dataset_id)
        return [c] if c is not None and c["source_uri"] == uri and v in self.served else []

    async def run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from server.core.ch import caller_shaped_var

        self.shaped.append(caller_shaped_var.get())
        self.own_reads.append(sequential_reads_var.get())
        self.ran.append((sql, params))
        if self.raises is not None:
            _raise_if_caller_cap(self.raises)  # as the client does with a cap's error
        self.versions.update(self.after_run)  # type: ignore[arg-type]
        for dataset_id, snapshot in self.snapshot_after_run.items():
            self.catalogs[dataset_id] = {**self.catalogs[dataset_id], "snapshot_id": snapshot}
        return self.rows


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Stores:
    return Stores(monkeypatch)


def _run(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "datasets": [{"alias": "o", "source_uri": "repo://orders.csv", "dataset_id": "orders"}],
        "aggregates": [{"fn": "count", "name": "n"}],
    }
    return asyncio.run(app.handle_knowledge_query(KnowledgeQueryInput(**{**base, **kw})))


def test_a_query_reads_the_live_snapshot_and_names_it_in_its_receipt(stores: Stores) -> None:
    out = _run()
    assert stores.shaped == [True]  # under the caller-shaped ceilings, as the reader user
    # Every lookup and the scan read the caller's own writes: a query right after an
    # ingest finds the snapshot it published, and every row of it.
    assert stores.own_reads == [True] * 5
    assert (out.rows, out.complete, out.incomplete, out.truncated) == ([[5]], True, [], False)
    (snap,) = out.receipt.snapshots
    assert (snap.alias, snap.snapshot_id, snap.revision, snap.schema_state) == (
        "o",
        "41",
        2,
        "inferred",
    )
    assert out.receipt.sql == stores.ran[0][0]
    assert out.receipt.params["p3"] == "41"
    assert (
        out.receipt.plan["datasets"][0]["dataset_id"] == "orders" and "org" not in out.receipt.plan
    )


def test_a_dataset_with_no_live_snapshot_is_not_found(stores: Stores) -> None:
    stores.versions["repo://orders.csv"] = None  # type: ignore[assignment]
    with pytest.raises(NotFoundError):
        _run()
    stores.versions["repo://orders.csv"] = 7
    with pytest.raises(NotFoundError):
        _run(datasets=[{"alias": "o", "source_uri": "repo://teams.csv", "dataset_id": "orders"}])
    assert stores.ran == []


def test_a_schema_that_needs_an_answer_is_refused_before_any_read(stores: Stores) -> None:
    asks = [{"code": "mixed_types", "column": "amount", "detail": "holds number, string values"}]
    stores.catalogs["orders"] = _catalog(
        "orders", ORDERS, schema_state="needs_clarification", clarifications=json.dumps(asks)
    )
    with pytest.raises(
        ValidationError, match=r"\(mixed_types amount\): approve it with knowledge.dataset_approve"
    ):
        _run()
    assert stores.ran == []


def test_a_plan_the_schema_refuses_is_a_validation_error(stores: Stores) -> None:
    with pytest.raises(ValidationError, match="has no column 'nope'"):
        _run(aggregates=[{"fn": "sum", "column": {"column": "nope"}, "name": "s"}])


def test_a_snapshot_replaced_while_the_statement_ran_is_never_released(stores: Stores) -> None:
    stores.after_run = {"repo://orders.csv": 8}
    with pytest.raises(PreconditionFailedError, match="changed while the query ran"):
        _run()
    stores.versions, stores.after_run = {"repo://orders.csv": 7}, {"repo://orders.csv": None}
    with pytest.raises(PreconditionFailedError):  # erased while it ran
        _run()
    # A retyping approval switches the snapshot under the same document version.
    stores.versions, stores.after_run = {"repo://orders.csv": 7}, {}
    stores.snapshot_after_run = {"orders": 99}
    with pytest.raises(PreconditionFailedError, match="dataset 'o' changed while the query ran"):
        _run()
    # A metadata edit moves the version but keeps the snapshot: the rows read still serve.
    stores.catalogs["orders"], stores.snapshot_after_run = CATALOGS["o"], {}
    stores.after_run, stores.served = {"repo://orders.csv": 8}, {7, 8}
    assert _run().rows == [[5]]


def test_a_publish_that_lands_as_the_query_looks_a_dataset_up_is_a_retry(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish writes the document's new version, then deletes the catalog rows
    of the one the query read: the lookup finds none, and that is a change."""
    stores.served = set()
    real = stores.live_versions

    async def publishing(org: str, namespace: str, uris: list[str]) -> dict[str, int]:
        found = await real(org, namespace, uris)
        stores.versions["repo://orders.csv"] = 8  # lands after this read
        return found

    monkeypatch.setattr(app.documents_repo, "live_versions", publishing)
    with pytest.raises(PreconditionFailedError, match="published again as the query read it"):
        _run()


def test_a_query_past_the_ceilings_says_how_to_narrow_a_query(stores: Stores) -> None:
    stores.raises = RuntimeError("Code: 158. DB::Exception: Limit for rows exceeded")
    with pytest.raises(ValidationError) as e:
        _run()
    assert str(e.value).endswith(
        "narrow it (a filter, fewer datasets, a join on a key that does not repeat)"
    )


def test_a_result_says_what_it_may_not_cover_and_when_more_matched(stores: Stores) -> None:
    stores.catalogs["orders"] = _catalog("orders", ORDERS, rejected_rows=3, complete=False)
    stores.catalogs["teams"] = _catalog("teams", TEAMS, rejected_rows=2)
    stores.rows = [{"c0": "a", "c1": Decimal("2.500000000"), "r0": 0, "r1": 4}] * 3
    out = _run(
        datasets=JOINED,
        joins=[{"left": _col("team", "o"), "right": _col("team", "t")}],
        aggregates=[],
        select=[_col("team", "o"), _col("budget", "t")],
        limit=2,
    )
    assert [c.name for c in out.columns] == ["o.team", "t.budget"]
    assert (out.rows, out.row_ids, out.truncated) == ([["a", 2.5]] * 2, [[0, 4]] * 2, True)
    assert not out.complete and out.incomplete == [
        "o: the extraction left part of the table out (knowledge.dataset_get)",
        "t: 2 of the source's rows were rejected, so not counted",
    ]
