"""`loom deploy-delta` reads the ledger and the query log on
both sides of a swap and flags only what moved past a coarse threshold."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.cli import reports
from server.insights.domain import deploy_delta as dd
from server.insights.store import deploy_delta as dds

SWAP = datetime(2026, 9, 16, 5, 52, 8, tzinfo=UTC)


class _CH:
    """Answers each statement by the table and columns it names."""

    def __init__(self, *, shapes_fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.shapes_fail = shapes_fail

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        if "groupUniqArray(build_commit)" in sql:
            return [
                {"side": "before", "builds": ["1f6398e26"], "calls": 20000},
                {"side": "after", "builds": ["64b52a75f"], "calls": 21000},
            ]
        if "error_code != ''" in sql:
            return [
                {
                    "operation": "memory.get",
                    "error_code": "RATE_LIMITED",
                    "n_before": 32,
                    "n_after": 0,
                },
                {
                    "operation": "code.repos",
                    "error_code": "AUTH_INSUFFICIENT",
                    "n_before": 0,
                    "n_after": 9,
                },
            ]
        if "FROM loom.search_trace_log" in sql:
            # One fact-only namespace whose windows filled with chunk ids after
            # the swap; one conversation namespace that did not move.
            return [
                {
                    "namespace": "clickgap-public",
                    "n_before": 260,
                    "n_after": 230,
                    "keys_before": 5200,
                    "keys_after": 4600,
                    "chunk_before": 0,
                    "chunk_after": 1150,
                    "summary_before": 0,
                    "summary_after": 0,
                    "other_before": 12,
                    "other_after": 9,
                },
                {
                    "namespace": "support-bot",
                    "n_before": 900,
                    "n_after": 880,
                    "keys_before": 18000,
                    "keys_after": 17600,
                    "chunk_before": 7200,
                    "chunk_after": 7100,
                    "summary_before": 300,
                    "summary_after": 310,
                    "other_before": 0,
                    "other_after": 0,
                },
            ]
        if "args['out_missing_n'] != ''" in sql:
            return [
                {
                    "operation": "memory.bulk_get",
                    "n_before": 400,
                    "n_after": 380,
                    "asked_before": 8000,
                    "asked_after": 7600,
                    "miss_before": 40,
                    "miss_after": 1900,
                },
                {
                    "operation": "memory.get",
                    "n_before": 3000,
                    "n_after": 2900,
                    "asked_before": 3000,
                    "asked_after": 2900,
                    "miss_before": 60,
                    "miss_after": 61,
                },
            ]
        if "operation = 'ui.vitals'" in sql:
            # Two routes with views on both sides, one regressed
            return [
                {
                    "route": "memories",
                    "n_before": 12,
                    "n_after": 9,
                    "lcp_before": 800.0,
                    "lcp_after": 1200.0,
                    "lt_before": 240.0,
                    "lt_after": 250.0,
                    "calls_before": 6.0,
                    "calls_after": 6.0,
                    "nodes_before": 2600.0,
                    "nodes_after": 2650.0,
                },
                {
                    "route": "admin/usage",
                    "n_before": 4,
                    "n_after": 5,
                    "lcp_before": 480.0,
                    "lcp_after": 470.0,
                    "lt_before": 110.0,
                    "lt_after": 112.0,
                    "calls_before": 7.0,
                    "calls_after": 6.0,
                    "nodes_before": 530.0,
                    "nodes_after": 530.0,
                },
            ]
        if "quantileIf(0.95)" in sql:
            return [
                {
                    "operation": "memory.ask",
                    "n_before": 800,
                    "n_after": 820,
                    "err_before": 1,
                    "err_after": 0,
                    "p50_before": 4900,
                    "p50_after": 5000,
                    "p95_before": 9000,
                    "p95_after": 9200,
                    "usd_before": 40.0,
                    "usd_after": 41.0,
                },
                {
                    "operation": "worker.entity_extractor",
                    "n_before": 1356,
                    "n_after": 1272,
                    "err_before": 0,
                    "err_after": 0,
                    "p50_before": 900,
                    "p50_after": 950,
                    "p95_before": 1754,
                    "p95_after": 2400,
                    "usd_before": 3.0,
                    "usd_after": 9.0,
                },
                {
                    "operation": "memory.get",
                    "n_before": 683,
                    "n_after": 0,
                    "err_before": 32,
                    "err_after": 0,
                    "p50_before": 40,
                    "p50_after": 0,
                    "p95_before": 70,
                    "p95_after": 0,
                    "usd_before": 0,
                    "usd_after": 0,
                },
                {
                    "operation": "grants.list",
                    "n_before": 50,
                    "n_after": 60,
                    "err_before": 0,
                    "err_after": 0,
                    "p50_before": 10,
                    "p50_after": 30,
                    "p95_before": 13,
                    "p95_after": 40,
                    "usd_before": 0,
                    "usd_after": 0,
                },
            ]
        if "normalized_query_hash IN" in sql:
            return [
                {
                    "shape": 41,
                    "sample": "SELECT (SELECT count() FROM loom.memory_embeddings …",
                    "log_comment": "",
                },
                {
                    "shape": 29,
                    "sample": "SELECT -- Memories that cannot be found …",
                    "log_comment": "",
                },
                {
                    "shape": 7,
                    "sample": "SELECT chunk_id FROM loom.code_chunks FINAL",
                    "log_comment": "worker.code_ingest",
                },
            ]
        if "normalized_query_hash AS shape" in sql:
            if self.shapes_fail:
                raise RuntimeError("clusterAllReplicas: not enough privileges")
            return [
                {
                    "shape": 41,
                    "n_before": 543,
                    "n_after": 540,
                    "bytes_before": 17_000_000_000,
                    "bytes_after": 17_000_000_000,
                    "ms_before": 1_183_000,
                    "ms_after": 30_000,
                },
                {
                    "shape": 29,
                    "n_before": 20,
                    "n_after": 4,
                    "bytes_before": 277_000_000_000,
                    "bytes_after": 55_000_000_000,
                    "ms_before": 136_000,
                    "ms_after": 28_000,
                },
                {
                    "shape": 7,
                    "n_before": 0,
                    "n_after": 53841,
                    "bytes_before": 0,
                    "bytes_after": 4_300_000_000,
                    "ms_before": 0,
                    "ms_after": 967_000,
                },
                {
                    "shape": 99,
                    "n_before": 100,
                    "n_after": 100,
                    "bytes_before": 1_000_000_000,
                    "bytes_after": 5_000_000_000,
                    "ms_before": 10_000,
                    "ms_after": 10_000,
                },
            ]
        raise AssertionError(f"unexpected statement: {sql[:80]}")


def _collect(ch: _CH, **kw: Any) -> dd.DeployDelta:
    return asyncio.run(dds.collect(ch, swap_at=SWAP, window_min=60, **kw))


def test_collect_reads_both_records_around_the_swap() -> None:
    ch = _CH()
    d = _collect(ch)
    assert d.builds_before == ["1f6398e26"] and d.builds_after == ["64b52a75f"]
    assert d.calls_before == 20000 and d.calls_after == 21000
    assert [o.operation for o in d.ops][:2] == ["memory.ask", "worker.entity_extractor"]
    assert {e.error_code for e in d.errors} == {"RATE_LIMITED", "AUTH_INSUFFICIENT"}
    assert {s.shape for s in d.shapes} == {41, 29, 7, 99}
    assert next(s for s in d.shapes if s.shape == 7).log_comment == "worker.code_ingest"
    # Every ledger statement is scoped to the deployment's own rows and to the
    # symmetric window; the swap instant is bound naive-UTC for DateTime.
    ledger = [(s, p) for s, p in ch.calls if "loom.agent_events" in s]
    # builds, ops, errors, page vitals, read outcomes
    assert len(ledger) == 5
    planes = [(s, p) for s, p in ch.calls if "loom.search_trace_log" in s]
    assert len(planes) == 1 and "INTERVAL {window:UInt32} MINUTE" in planes[0][0]
    for sql, params in ledger:
        assert dds.DEPLOYMENT_FILTER in sql
        assert "INTERVAL {window:UInt32} MINUTE" in sql
        assert params["swap"] == datetime(2026, 9, 16, 5, 52, 8) and params["swap"].tzinfo is None
        assert params["window"] == 60
    # The query-log half reads all replicas of the named cluster, filters the
    # deployment's sessions by os_user, and reads `query` only for the kept hashes.
    shapes = [(s, p) for s, p in ch.calls if "normalized_query_hash AS shape" in s][0]
    assert "clusterAllReplicas('default', system.query_log)" in shapes[0]
    assert shapes[1]["ua"] == "%os_user:loom%" and "query" not in shapes[0].split("FROM")[
        0
    ].replace("query_duration_ms", "").replace("normalized_query_hash", "")
    samples = [(s, p) for s, p in ch.calls if "normalized_query_hash IN" in s][0]
    assert sorted(samples[1]["shapes"]) == [7, 29, 41, 99]
    assert "LIMIT 1 BY shape" in samples[0]


def test_flags_name_only_what_moved_past_the_thresholds() -> None:
    d = _collect(_CH())
    fl = dd.flags(d)
    text = "\n".join(fl)
    # A new error code that appears after the swap.
    assert "NEW ERROR code.repos AUTH_INSUFFICIENT: 0 → 9" in text
    # RATE_LIMITED went away: not a flag.
    assert "RATE_LIMITED" not in text
    # p95 +37% on a busy worker op, and its cost per call tripled.
    assert "P95 worker.entity_extractor: 1754 → 2400 ms" in text
    assert "COST/CALL worker.entity_extractor" in text
    # memory.ask moved 2%: quiet. grants.list tripled its p95 but is under the call floor.
    assert "memory.ask" not in text and "grants.list" not in text
    # An operation that stopped entirely is worth a line.
    assert "STOPPED memory.get: 683 calls before, none after" in text
    # Shapes: the fast probe got 40× cheaper (no flag), the census got rarer
    # (no flag), a new shape appeared, and one shape's bytes/run went 10 → 50 MB.
    assert "NEW SHAPE 7: 53841 runs" in text and "worker.code_ingest" in text
    assert "SHAPE 99: 10 → 50 MB/run" in text
    assert "SHAPE 41" not in text and "SHAPE 29" not in text


def test_render_leads_with_flags_and_carries_every_section() -> None:
    d = _collect(_CH())
    out = dd.render(d)
    assert out.startswith("# deploy delta — swap at 2026-09-16T05:52:08+00:00 ±60 min")
    assert "builds before: 1f6398e26 (20000 calls) | after: 64b52a75f (21000 calls)" in out
    assert out.index("## flags") < out.index("## operations") < out.index("## error codes")
    assert "## ClickHouse statement shapes" in out
    assert "| memory.ask | 800 → 820 | 1 → 0 | 4900 → 5000 | 9000 → 9200 | 40.00 → 41.00 |" in out
    assert "| 41 | 543 → 540 | 17.0 → 17.0 | 1183 → 30 | 2179 → 56 |" in out
    # A side with no calls renders a dash, not NaN.
    d.ops[0].p50_after = float("nan")
    assert "| memory.ask | 800 → 820 | 1 → 0 | 4900 → — |" in dd.render(d)
    j = json.loads(d.to_json())
    assert j["swap_at"].startswith("2026-09-16T05:52:08") and j["flags"] == dd.flags(d)


def test_a_failed_query_log_read_leaves_the_ledger_half_standing() -> None:
    d = _collect(_CH(shapes_fail=True))
    assert d.ops and d.errors and d.shapes == []
    assert d.shapes_error.startswith("RuntimeError: clusterAllReplicas")
    assert "query_log unavailable: RuntimeError" in dd.render(d)
    assert not any(f.startswith(("NEW SHAPE", "SHAPE")) for f in dd.flags(d))


def test_the_shape_reads_carry_and_bind_the_analyst_filter() -> None:
    ch = _CH()
    _collect(ch)
    reads = [(sql, params) for sql, params in ch.calls if "system.query_log" in sql]
    assert reads
    for sql, params in reads:
        assert "user != {analyst:String}" in sql
        assert params["analyst"] == "loom_analyst"


def test_no_cluster_reads_the_connected_replica_only() -> None:
    assert "FROM system.query_log" in dds.shapes_sql(
        ""
    ) and "clusterAllReplicas" not in dds.shapes_sql("")
    assert "clusterAllReplicas('prod', system.query_log)" in dds.samples_sql("prod")


@pytest.mark.parametrize("bad", ["x') UNION ALL SELECT 1 --", "a b", "prod'"])
def test_a_cluster_name_that_is_not_a_plain_name_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="not a cluster name"):
        dds.shapes_sql(bad)
    with pytest.raises(ValueError, match="not a cluster name"):
        dds.samples_sql(bad)


def test_parse_swap_at_accepts_now_iso_and_deployed_json(tmp_path: Any) -> None:
    assert reports.parse_swap_at("2026-09-16T05:52:08Z") == SWAP
    assert reports.parse_swap_at("2026-09-16T05:52:08+00:00") == SWAP
    assert reports.parse_swap_at("2026-09-16 05:52:08").tzinfo is UTC
    p = tmp_path / "deployed.json"
    p.write_text(json.dumps({"deployed_at": "2026-09-16T05:52:08Z", "colour": "green"}))
    assert reports.parse_swap_at(str(p)) == SWAP
    before = datetime.now(UTC)
    assert reports.parse_swap_at("now") >= before


def test_cli_registers_deploy_delta() -> None:
    import argparse

    from server.cli import reports as cli

    parser = argparse.ArgumentParser()
    cli._add_deploy_delta_parser(parser.add_subparsers())
    ns = parser.parse_args(
        ["deploy-delta", "--swap-at", "now", "--window-min", "30", "--cluster", ""]
    )
    assert ns.func is cli._cmd_deploy_delta
    assert ns.window_min == 30 and ns.cluster == "" and ns.ch_os_user == "loom"
    assert parser.parse_args(["deploy-delta"]).swap_at == "var/deployed.json"


def test_an_empty_window_is_a_warning_not_a_quiet_page() -> None:
    class _Empty(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            return []

    d = _collect(_Empty())
    assert d.empty
    out = dd.render(d)
    assert "WARNING: no deployment rows in the window" in out
    assert not dd.flags(d)


def test_page_vitals_ride_the_report_and_flag_a_slower_paint() -> None:
    """The dashboard's ui.vitals rows per route,
    before → after, and a flag when a route's p50 paint or long tasks grow
    by more than a third with views on both sides."""
    d = _collect(_CH())
    assert [v.route for v in d.vitals] == ["memories", "admin/usage"]
    out = dd.render(d)
    assert (
        out.index("## error codes")
        < out.index("## page vitals")
        < out.index("## ClickHouse statement shapes")
    )
    assert "| memories | 12 → 9 | 800 → 1200 | 240 → 250 | 0 → 0 | 6 → 6 | 2600 → 2650 |" in out
    text = "\n".join(dd.flags(d))
    assert "PAGE memories: p50 LCP 800 → 1200 ms over 12 → 9 views" in text
    assert "admin/usage" not in text  # moved 2 %: quiet
    assert "long tasks" not in text  # 240 → 250 is not a third


def test_result_planes_and_read_outcomes_ride_the_report_and_flag_what_moved() -> None:
    """A swap can change WHAT searches return (a fact-only namespace whose
    windows fill with chunks) and what reads answer (bulk_get missing a
    quarter of its keys) with every latency and error count flat. Both are
    rows here and flags when they move."""
    d = _collect(_CH())
    assert [p.namespace for p in d.planes] == ["clickgap-public", "support-bot"]
    assert [r.operation for r in d.reads] == ["memory.bulk_get", "memory.get"]
    out = dd.render(d)
    assert (
        out.index("## page vitals")
        < out.index("## result planes")
        < out.index("## read outcomes")
        < out.index("## ClickHouse statement shapes")
    )
    assert (
        "| clickgap-public | 260 → 230 | 5200 → 4600 | 0 → 1150 | 0 → 0 | 12 → 9 | 0% → 25% |"
        in out
    )
    assert "| memory.bulk_get | 400 → 380 | 8000 → 7600 | 40 → 1900 | 0% → 25% |" in out
    text = "\n".join(dd.flags(d))
    assert "PLANE MIX clickgap-public: chunk keys 0% → 25% of returned" in text
    assert "support-bot" not in text  # 40% → 40%: quiet
    assert "READ MISSES memory.bulk_get: 0% → 25% of keys asked not found" in text
    assert "READ MISSES memory.get" not in text  # 2% → 2%: quiet


def test_no_recorded_searches_and_no_outcome_columns_are_lines_not_errors() -> None:
    d = _collect(_CH())
    d.planes, d.reads = [], []
    out = dd.render(d)
    assert "no recorded searches in the window" in out
    assert "no read-outcome columns in the window" in out


def test_bench_traffic_is_not_the_deployments_callers() -> None:
    """A paired replay on the live colour (traffic_class='bench') can inflate
    a swap's memory.search P95 far above what callers see; every ledger read
    excludes bench rows."""
    ch = _CH()
    _collect(ch)
    for sql, _ in ch.calls:
        if "loom.agent_events" in sql:
            assert "traffic_class != 'bench'" in sql, sql[:120]


def test_a_sparse_query_log_before_window_suppresses_new_shape_flags() -> None:
    """A sparse before-window (e.g. 786 statements vs 14,250 after) would make
    many shapes 'NEW'; the report says the window is sparse and keeps only
    per-shape deltas."""
    d = _collect(_CH())
    for s in d.shapes:
        s.n_before = 0
    d.shapes.append(
        dd.ShapeRow(
            shape=5,
            n_before=0,
            n_after=600,
            bytes_before=0,
            bytes_after=0,
            ms_before=0,
            ms_after=900_000,
            sample="worker.x",
        )
    )
    assert d.shapes_before_sparse is True
    fl = dd.flags(d)
    assert any(f.startswith("QUERY_LOG SPARSE: 0 statements before vs") for f in fl)
    assert not any(f.startswith("NEW SHAPE ") for f in fl)
    # a healthy before-window keeps the flag
    for s in d.shapes:
        s.n_before = 100
    d.shapes[-1].n_before = 0
    assert d.shapes_before_sparse is False
    assert "NEW SHAPE 5:" in "\n".join(dd.flags(d))


def test_no_dashboard_views_is_a_line_not_an_error() -> None:
    d = _collect(_CH())
    d.vitals = []
    assert "no dashboard views in the window" in dd.render(d)
