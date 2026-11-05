"""knowledge.query against a real server, ClickHouse and extractor: CSV and
XLSX files ingested into datasets, then calculated over with typed plans.

The reference queries check filters, joins, totals, nulls, decimal precision,
time boundaries and saved formula values, the receipt's snapshots, and that
rows of another org or namespace never reach a count.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

ORDERS = (
    b"order_id,team,amount,placed_at,shipped\n"
    b"1,platform,120.50,2026-01-15T10:00:00,true\n"
    b"2,platform,0.10,2026-01-31T23:59:59,false\n"
    b"3,search,99999999999999999999.123456789,2026-02-01T00:00:00,true\n"
    b"4,analytics,,2026-02-10T08:00:00,\n"
    b"5,search,0.20,2026-03-01T00:00:00,true\n"
)
TEAMS = b"team,lead,budget\nplatform,ana,1000\nsearch,bo,500\nanalytics,cy,250\n"


@pytest.fixture
async def csv_routed() -> None:
    from server.knowledge.app.extractors import available_formats

    if "csv" not in await available_formats():
        pytest.skip("no configured document extractor declares csv")


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _publish(
    c: httpx.AsyncClient, org: str, ns: str, uri: str, data: bytes, filename: str
) -> dict[str, Any]:
    """Upload and ingest `data`, run the job here, and return its one dataset."""
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

    up = await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert up.status_code == 201, up.text
    job = await _op(
        c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
        source_uri=uri,
    )  # fmt: skip
    ch = get_client()
    await ch.connect()
    try:
        row = await ingestions.get_ingestion(org, ns, job["ingestion_id"])
        assert row is not None
        assert await ingest_run.take_and_run(row) is not None
    finally:
        await ch.disconnect()
    got = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri)
    (d,) = got["datasets"]
    return dict(d)


async def _query(c: httpx.AsyncClient, org: str, ns: str, **plan: Any) -> httpx.Response:
    return await c.post("/v1/knowledge.query", json={"org": org, "namespace": ns, **plan})


def _col(column: str, dataset: str = "") -> dict[str, str]:
    return {"dataset": dataset, "column": column}


@pytest.mark.usefixtures("csv_routed")
@pytest.mark.asyncio
async def test_reference_queries(control_plane_url: str) -> None:
    org, ns = unique_org("query"), "data"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        orders = await _publish(c, org, ns, "repo://orders.csv", ORDERS, "orders.csv")
        teams = await _publish(c, org, ns, "repo://teams.csv", TEAMS, "teams.csv")
        o = {"alias": "o", "source_uri": "repo://orders.csv", "dataset_id": orders["dataset_id"]}
        t = {"alias": "t", "source_uri": "repo://teams.csv", "dataset_id": teams["dataset_id"]}
        results: dict[str, Any] = {}
        plans = {
            "totals": dict(datasets=[o], aggregates=[
                {"fn": "count", "name": "rows"},
                {"fn": "count", "column": _col("amount"), "name": "priced"},
                {"fn": "sum", "column": _col("amount"), "name": "total"},
                {"fn": "avg", "column": _col("amount"), "name": "mean"},
                {"fn": "count_distinct", "column": _col("team"), "name": "teams"},
            ]),
            "per_team": dict(datasets=[o], group_by=[{"column": _col("team")}], aggregates=[
                {"fn": "sum", "column": _col("amount"), "name": "total"},
                {"fn": "count", "name": "orders"},
            ]),
            "january": dict(datasets=[o], where=[
                {"column": _col("placed_at"), "op": "ge", "value": "2026-01-01T00:00:00"},
                {"column": _col("placed_at"), "op": "lt", "value": "2026-02-01T00:00:00"},
            ], aggregates=[{"fn": "count", "name": "n"}]),
            "monthly": dict(datasets=[o], group_by=[
                {"column": _col("placed_at"), "bucket": "month", "name": "month"}
            ], aggregates=[{"fn": "count", "name": "n"}]),
            "not_listed": dict(datasets=[o], where=[
                {"column": _col("amount"), "op": "not_in", "values": ["0.1"]}
            ], aggregates=[{"fn": "count", "name": "n"}]),
            "unpriced": dict(datasets=[o], where=[{"column": _col("amount"), "op": "is_null"}],
                             aggregates=[{"fn": "count", "name": "n"}]),
            "joined": dict(datasets=[o, t], joins=[
                {"left": _col("team", "o"), "right": _col("team", "t")}
            ], where=[{"column": _col("lead", "t"), "op": "eq", "value": "ana"}],
                aggregates=[{"fn": "sum", "column": _col("amount", "o"), "name": "total"}]),
            "listed": dict(datasets=[o], where=[
                {"column": _col("amount"), "op": "in", "values": ["0.1", 0.2, "120.5"]},
                {"column": _col("team"), "op": "starts_with", "value": "plat"},
                {"column": _col("placed_at"), "op": "ne", "value": "2026-01-15T10:00:00Z"},
            ], aggregates=[{"fn": "count", "name": "n"}, {"fn": "max", "column": _col("placed_at"),
                                                           "name": "last"}]),
            "rows": dict(datasets=[o], where=[{"column": _col("team"), "op": "eq", "value": "search"}],
                         select=[_col("order_id"), _col("amount")],
                         order_by=[{"by": "amount", "desc": True}], limit=1),
        }  # fmt: skip
        for name, plan in plans.items():
            r = await _query(c, org, ns, **plan)
            assert r.status_code == 200, (name, r.text)
            results[name] = r.json()
        # The CSV reader reads the 30-digit amount as a double (1e20): its digits are
        # Parquet's to keep (test_a_parquet_decimal_sums_exactly).
        assert results["totals"]["rows"] == [
            [5, 4, "100000000000000000120.8", "25000000000000000030.2", 3]
        ]
        # A sum of no values (analytics' one order has no amount) is null, not 0.
        assert results["per_team"]["rows"] == [
            ["analytics", None, 1], ["platform", 120.6, 2], ["search", "100000000000000000000.2", 2]
        ]  # fmt: skip
        assert results["january"]["rows"] == [[2]]  # 23:59:59 on the 31st is January's
        assert results["monthly"]["rows"] == [
            ["2026-01-01", 2], ["2026-02-01", 2], ["2026-03-01", 1]
        ]  # fmt: skip
        assert results["unpriced"]["rows"] == [[1]]
        assert results["not_listed"]["rows"] == [[3]]  # a null amount is in no list, nor out of one
        # 0.1 and 120.5 are platform's; 120.5's order is the one placed at 10:00 on the 15th.
        assert results["listed"]["rows"] == [[1, "2026-01-31T23:59:59"]]
        assert results["joined"]["rows"] == [[120.6]]
        rows = results["rows"]
        assert (rows["rows"], rows["row_ids"], rows["truncated"]) == (
            [[3, "100000000000000000000"]],
            [[2]],
            True,
        )
        assert all(r["complete"] and r["incomplete"] == [] for r in results.values())
        receipt = results["joined"]["receipt"]
        assert [(x["alias"], x["snapshot_id"], x["revision"]) for x in receipt["snapshots"]] == [
            ("o", orders["snapshot_id"], 1), ("t", teams["snapshot_id"], 1)
        ]  # fmt: skip
        assert org not in receipt["sql"] and "ana" not in receipt["sql"]  # values are parameters
        assert "ana" in receipt["params"].values()


@pytest.mark.usefixtures("csv_routed")
@pytest.mark.asyncio
async def test_rows_of_another_org_or_namespace_never_reach_a_count(control_plane_url: str) -> None:
    org, other = unique_org("query-a"), unique_org("query-b")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        mine = await _publish(c, org, "data", "repo://orders.csv", ORDERS, "orders.csv")
        # The same file, so the same dataset id, under another namespace and another org.
        await _publish(c, org, "elsewhere", "repo://orders.csv", ORDERS, "orders.csv")
        theirs = await _publish(c, other, "data", "repo://orders.csv", ORDERS, "orders.csv")
        assert theirs["dataset_id"] == mine["dataset_id"]
        plan: dict[str, Any] = {
            "datasets": [{"source_uri": "repo://orders.csv", "dataset_id": mine["dataset_id"]}],
            "aggregates": [{"fn": "count", "name": "n"}],
        }
        r = await _query(c, org, "data", **plan)
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [[5]]  # not 15
        # An org with no such dataset finds none, though the id is another org's.
        r = await _query(c, unique_org("query-c"), "data", **plan)
        assert r.status_code == 404, r.text


@pytest.mark.usefixtures("csv_routed")
@pytest.mark.asyncio
async def test_a_dataset_whose_schema_needs_an_answer_is_refused_until_approved(
    control_plane_url: str,
) -> None:
    org, ns, uri = unique_org("query-ask"), "data", "repo://budget.csv"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        data = b"team,amount\nplatform,120.5\nanalytics,n/a\nsearch,80\n"
        d = await _publish(c, org, ns, uri, data, "budget.csv")
        assert d["schema_state"] == "needs_clarification"
        plan = {
            "datasets": [{"source_uri": uri, "dataset_id": d["dataset_id"]}],
            "aggregates": [{"fn": "count", "name": "n"}],
        }
        r = await _query(c, org, ns, **plan)
        assert r.status_code == 422 and "knowledge.dataset_approve" in r.text, r.text
        await _op(
            c, "knowledge.dataset_approve", org=org, namespace=ns, source_uri=uri,
            dataset_id=d["dataset_id"], snapshot_id=d["snapshot_id"], columns={"amount": "number"},
        )  # fmt: skip
        r = await _query(c, org, ns, **plan)
        assert r.status_code == 200, r.text
        out = r.json()
        # The row whose amount is not a number was rejected: the count says it may not cover it.
        assert out["rows"] == [[2]] and not out["complete"]
        assert out["incomplete"] == ["d: 1 of the source's rows were rejected, so not counted"]


@pytest.mark.asyncio
async def test_a_parquet_decimal_sums_exactly(control_plane_url: str) -> None:
    import io
    from decimal import Decimal

    import pyarrow as pa
    import pyarrow.parquet as pq

    from server.knowledge.app.extractors import available_formats

    if "parquet" not in await available_formats():
        pytest.skip("no configured document extractor declares parquet")
    import datetime as dt

    amounts = [Decimal("99999999999999999999.123456789"), Decimal("0.000000001"), Decimal(0)]
    table = pa.table(
        {
            "amount": pa.array(amounts, pa.decimal128(38, 9)),
            "settled": [True, True, False],
            "due": pa.array([dt.date(2026, 1, 31), dt.date(2026, 2, 1), None], pa.date32()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    org, ns, uri = unique_org("query-dec"), "data", "repo://ledger.parquet"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        d = await _publish(c, org, ns, uri, buf.getvalue(), "ledger.parquet")
        r = await _query(
            c, org, ns, datasets=[{"source_uri": uri, "dataset_id": d["dataset_id"]}],
            aggregates=[
                {"fn": "sum", "column": _col("amount"), "name": "total"},
                {"fn": "avg", "column": _col("amount"), "name": "mean"},
                {"fn": "max", "column": _col("amount"), "name": "top"},
            ],
        )  # fmt: skip
        assert r.status_code == 200, r.text
        # Every digit: a double would make the total 1e20.
        assert r.json()["rows"] == [
            ["99999999999999999999.12345679", "33333333333333333333.041152263",
             "99999999999999999999.123456789"]
        ]  # fmt: skip
        r = await _query(
            c, org, ns, datasets=[{"source_uri": uri, "dataset_id": d["dataset_id"]}],
            where=[{"column": _col("settled"), "op": "eq", "value": True},
                   {"column": _col("amount"), "op": "gt", "value": 0}],
            aggregates=[{"fn": "count", "name": "n"}, {"fn": "max", "column": _col("due"),
                                                       "name": "last"}],
        )  # fmt: skip
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["rows"] == [[2, "2026-02-01"]]  # a date, as its column's type says
        assert [c_["type"] for c_ in out["columns"]] == ["number", "date"]
        assert out["receipt"]["params"]["p5"] == "0.000000000"  # plain digits, not 0E-9


@pytest.mark.asyncio
async def test_a_formula_column_is_calculated_over_its_saved_values(control_plane_url: str) -> None:
    from server.knowledge.app.extractors import available_formats
    from tests.fixtures.xlsx_files import cell, sheet, workbook

    if "xlsx" not in await available_formats():
        pytest.skip("no configured document extractor declares xlsx")
    rows = {
        1: [cell("A1", "item", t="inlineStr"), cell("B1", "total", t="inlineStr")],
        # The saved values are what the workbook last calculated, not B2's formula run again.
        2: [cell("A2", "disk", t="inlineStr"), cell("B2", "30", f="10*2")],
        3: [cell("A3", "cpu", t="inlineStr"), cell("B3", "12.5", f="5*2.5")],
    }
    org, ns, uri = unique_org("query-xlsx"), "data", "repo://costs.xlsx"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        d = await _publish(c, org, ns, uri, workbook([("Costs", sheet(rows))]), "costs.xlsx")
        r = await _query(
            c, org, ns, datasets=[{"source_uri": uri, "dataset_id": d["dataset_id"]}],
            aggregates=[{"fn": "sum", "column": _col("total"), "name": "total"}],
        )  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [[42.5]] and r.json()["complete"]
