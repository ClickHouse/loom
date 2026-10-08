"""Cost.summary reads the once-a-minute roll-up for the window
plus the live tail of agent_events newer than the roll-up's mark, in one
statement, so a readout is exact at any instant. CH-free: a fake client
records the statement and answers with merged rows."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from server.insights.api.models_cost import CostSummaryInput
from server.insights.app import cost as h
from server.insights.store import cost as cost_store


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.rows


def _run(monkeypatch, ch: _CH, **kw) -> Any:
    monkeypatch.setattr(h, "get_client", lambda: ch)
    inp = CostSummaryInput(
        org="o", namespace="n", from_date=date(2026, 9, 1), to_date=date(2026, 9, 14), **kw
    )
    return asyncio.run(h.handle_cost_summary(inp))


def test_rollup_read_unions_the_live_tail_and_merges(monkeypatch) -> None:
    ch = _CH(
        [
            {
                "operation": "memory.ask",
                "cost_usd": 1.25,
                "tokens": 100,
                "embed_tokens": 3,
                "ops": 7,
                "errors": 1,
                "lat_q": [10.0, 20.0, 30.0],
            },
        ]
    )
    out = _run(monkeypatch, ch)
    sql, params = ch.calls[0]
    # the roll-up's states for the window ...
    assert (
        "FROM loom.agent_cost_rollup_daily WHERE org = {org:String} AND day BETWEEN {from_d:Date} AND {to_d:Date}"
        in sql
    )
    assert cost_store.ROLLUP_STATES in sql
    # ... UNION ALL the view's aggregation over the events newer than the mark ...
    assert "UNION ALL" in sql
    assert (
        "FROM loom.agent_events WHERE org = {org:String} AND ts > (SELECT max(ts_hwm) FROM loom.agent_cost_rollup_daily)"
        in sql
    )
    assert "toDate(ts) BETWEEN {from_d:Date} AND {to_d:Date}" in sql
    assert cost_store.TAIL_STATES in sql and f"GROUP BY {cost_store.ROLLUP_DIMS}" in sql
    # ... merged once, grouped by the caller's dimension
    assert sql.count("sumMerge(cost_usd_state)") == 1 and "countMerge(ops_state)" in sql
    assert sql.rstrip().endswith("GROUP BY operation ORDER BY ops DESC LIMIT 5000")
    assert "PREWHERE" not in sql
    assert params["ns"] == "n" and sql.count("namespace = {ns:String}") == 2  # both legs scoped
    assert out.total_ops == 7 and out.total_errors == 1 and out.total_cost_usd == 1.25
    assert out.rows[0].latency_p95_ms == 20.0


def test_every_filter_reaches_both_legs(monkeypatch) -> None:
    ch = _CH([])
    _run(monkeypatch, ch, agent="a", operation="memory.set", group_by=["namespace", "day"])
    sql, params = ch.calls[0]
    for pred in ("agent = {agent:String}", "operation = {op:String}"):
        assert sql.count(pred) == 2, pred
    assert params["agent"] == "a" and params["op"] == "memory.set"
    assert "GROUP BY namespace, day ORDER BY" in sql


def test_events_only_dimensions_keep_the_direct_scan(monkeypatch) -> None:
    ch = _CH([])
    _run(monkeypatch, ch, group_by=["model"])
    sql, _ = ch.calls[0]
    assert "agent_cost_rollup_daily" not in sql and "UNION ALL" not in sql
    assert "FROM loom.agent_events" in sql


def test_process_dimensions_scan_events_and_name_the_writer(monkeypatch) -> None:
    """`instance_label` / `build_commit` group the spend
    by the PROCESS that wrote the row (the deployment against each bench
    server); they live only on agent_events, so the read is the direct scan,
    and a filter on the label reaches the WHERE."""
    ch = _CH(
        [
            {
                "instance_label": "deploy:web:blue",
                "build_commit": "f679c4076",
                "cost_usd": 0.2,
                "tokens": 10,
                "embed_tokens": 0,
                "ops": 5,
                "errors": 0,
                "lat_q": [1.0, 2.0, 3.0],
            },
            {
                "instance_label": "iso:w6m1:7864",
                "build_commit": "",
                "cost_usd": 40.0,
                "tokens": 900,
                "embed_tokens": 0,
                "ops": 1949,
                "errors": 0,
                "lat_q": [1.0, 2.0, 3.0],
            },
        ]
    )
    out = _run(monkeypatch, ch, group_by=["instance_label", "build_commit"])
    sql, _params = ch.calls[0]
    assert "agent_cost_rollup_daily" not in sql and "FROM loom.agent_events" in sql
    assert "GROUP BY instance_label, build_commit ORDER BY" in sql
    assert [r.instance_label for r in out.rows] == ["deploy:web:blue", "iso:w6m1:7864"]
    assert out.rows[0].build_commit == "f679c4076" and out.total_cost_usd == 40.2

    ch2 = _CH([])
    _run(monkeypatch, ch2, instance_label="deploy:web:blue")
    sql2, params2 = ch2.calls[0]
    assert "agent_cost_rollup_daily" not in sql2
    assert "instance_label = {ilabel:String}" in sql2 and params2["ilabel"] == "deploy:web:blue"
