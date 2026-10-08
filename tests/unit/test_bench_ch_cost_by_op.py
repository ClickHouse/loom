"""bench/ch_cost_by_op.py — the query-log join the query tag
makes possible: ClickHouse cost per loom operation, and one request's queries."""

from __future__ import annotations

from bench.ch_cost_by_op import (
    build_report,
    by_op_sql,
    op_of,
    render,
    request_of,
    request_sql,
)


def test_tag_parsing_matches_the_transports_and_workers() -> None:
    assert op_of("memory.search 3f2a-1") == "memory.search"
    assert request_of("memory.search 3f2a-1") == "3f2a-1"
    assert (
        op_of("worker.usage_fold") == "worker.usage_fold" and request_of("worker.usage_fold") == ""
    )
    assert op_of("") == "<untagged>" and op_of("   ") == "<untagged>"


def test_by_op_sql_derives_op_and_request_the_same_way() -> None:
    sql = by_op_sql(2.5, "loom")
    assert "splitByChar(' ', log_comment)[1]" in sql  # op = first token
    assert "if(log_comment = '', '<untagged>'" in sql  # untagged is a row, not a gap
    assert "uniqExactIf(splitByChar(' ', log_comment)[2]" in sql  # requests = distinct ids
    assert "INTERVAL 9000 SECOND" in sql and "has(databases, 'loom')" in sql
    assert "type = 'QueryFinish'" in sql and "system.%" in sql


def test_request_sql_lists_one_request_in_order() -> None:
    sql = request_sql("abc")
    assert "endsWith(log_comment, {rid:String})" in sql and "ORDER BY at" in sql


def _row(op: str, queries: int, requests: int, total_ms: float, **kw: float) -> dict[str, object]:
    return {
        "op": op,
        "queries": queries,
        "requests": requests,
        "total_ms": total_ms,
        "p50_ms": kw.get("p50", 10.0),
        "p95_ms": kw.get("p95", 50.0),
        "read_rows_avg": kw.get("rows", 100.0),
        "read_mib_total": kw.get("mib", 1.5),
        "peak_mem_mib_p95": kw.get("mem", 30.0),
    }


def test_build_report_orders_by_time_and_computes_shares() -> None:
    rep = build_report(
        24,
        [
            _row("memory.search", 1000, 250, 60_000.0),
            _row("worker.usage_fold", 10, 0, 30_000.0),
            _row("<untagged>", 100, 0, 10_000.0),
        ],
    )
    assert [r.op for r in rep.rows] == ["memory.search", "worker.usage_fold", "<untagged>"]
    assert rep.rows[0].share_of_time == 0.6 and rep.rows[0].queries_per_request == 4.0
    assert rep.rows[1].queries_per_request == 0.0  # workers have no request
    assert rep.untagged_share == round(100 / 1110, 4)
    j = rep.to_json()
    assert j["ops"][0]["queries_per_request"] == 4.0 and j["untagged_share"] == rep.untagged_share


def test_render_is_one_line_per_op_with_header() -> None:
    rep = build_report(1, [_row("memory.ask", 5, 5, 500.0)])
    text = render(rep)
    lines = text.splitlines()
    assert lines[0].startswith("ClickHouse cost by operation, last 1 h")
    assert "memory.ask" in lines[-1] and "100.0%" in lines[-1]


def test_empty_window_renders_without_division_errors() -> None:
    rep = build_report(1, [])
    assert rep.untagged_share == 0.0 and render(rep).count("\n") == 2


def test_shapes_sql_groups_by_normalized_hash_and_orders_by_bytes() -> None:
    from bench.ch_cost_by_op import shapes_sql

    sql = shapes_sql(24, "loom", limit=7)
    assert "normalizedQueryHash(query)" in sql
    assert "GROUP BY op, shape" in sql and "ORDER BY sum(read_bytes) DESC" in sql
    assert "LIMIT 7" in sql and "INTERVAL 86400 SECOND" in sql
    assert "result_rows" in sql  # bytes/call against result rows/call is the tell


def test_render_shapes_puts_bytes_per_call_beside_result_rows() -> None:
    from bench.ch_cost_by_op import render_shapes

    rows = [
        {
            "op": "worker.code_ingest",
            "shape": "9824547949083763495",
            "calls": 7061,
            "read_gib_total": 528.5,
            "read_mib_per_call": 76.6,
            "read_rows_per_call": 8342,
            "result_rows_per_call": 5.0,
            "p50_ms": 77,
            "sample": "SELECT * FROM loom.code_chunks FINAL WHERE (org = 'x')",
        }
    ]
    text = render_shapes(rows, 24)
    assert "worker.code_ingest" in text and "9824547949083763495" in text
    assert "76.6" in text and "5.0" in text and "code_chunks FINAL" in text


def test_explain_target_sql_binds_the_shape_as_a_parameter() -> None:
    from bench.ch_cost_by_op import explain_target_sql

    sql = explain_target_sql(48)
    assert "{shape:String}" in sql and "ORDER BY event_time DESC" in sql


def test_ch_request_keeps_credentials_out_of_the_url_and_runs_read_only() -> None:
    from bench.ch_cost_by_op import ch_request

    req = ch_request("http://ch:8123/", "u", "s3cret", "SELECT 1", {"rid": "abc"})
    assert "s3cret" not in req.full_url and "password" not in req.full_url
    assert req.get_header("X-clickhouse-key") == "s3cret"
    assert req.get_header("X-clickhouse-user") == "u"
    assert "readonly=2" in req.full_url and "param_rid=abc" in req.full_url
    assert req.full_url.startswith("http://ch:8123/?")
